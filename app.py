import os
import sys
import time
import threading
import json
import sqlite3
import re
import webbrowser
import io
import subprocess
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, jsonify, send_from_directory, request, send_file
import fitz  # PyMuPDF

# ---------------- Configuration & Setup ----------------
DATA_DIR = os.path.join(os.getenv('APPDATA', os.getcwd()), "AdvancedPDF_Data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "library.db")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
TREE_FILE = os.path.join(DATA_DIR, "tree.json")

if getattr(sys, 'frozen', False):
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    static_folder = os.path.join(sys._MEIPASS, 'static')
    app = Flask(__name__, template_folder=template_folder, static_folder=static_folder)
else:
    app = Flask(__name__, static_folder='static', template_folder='.')

current_config = {'root_paths': [], 'theme_mode': 0}
scan_status = {'is_scanning': False, 'total_files': 0, 'done': False}
indexer_status = {'is_indexing': False, 'total_files': 0, 'processed': 0, 'current': '', 'done': False, 'stop_flag': False}
server_status = {'last_heartbeat': time.time()}

# ---------------- Database Helpers ----------------
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            # הפעלת מצב WAL לביצועים טובים יותר ומניעת נעילות
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            
            c.execute('''CREATE TABLE IF NOT EXISTS files (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            path TEXT UNIQUE, filename TEXT, mod_time REAL, scanned INTEGER DEFAULT 0)''')
            try:
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(file_id UNINDEXED, page_num UNINDEXED, content)")
            except Exception: pass
            c.execute('''CREATE TABLE IF NOT EXISTS favorites (path TEXT PRIMARY KEY)''')
            c.execute('''CREATE TABLE IF NOT EXISTS history (path TEXT PRIMARY KEY, last_access REAL)''')
            conn.commit()
    except Exception as e:
        print(f"DB Init Error: {e}")
        
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                current_config.update(json.load(f))
        except: pass

def save_config():
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(current_config, f)
    except: pass

# ---------------- File Scanning Logic ----------------
def scan_files_on_disk(root_paths):
    valid_roots = []
    sorted_paths = sorted([os.path.normpath(p).replace('\\', '/') for p in root_paths], key=len)
    for p in sorted_paths:
        if not any(p.startswith(v + '/') for v in valid_roots) and p not in valid_roots:
            valid_roots.append(p)

    # שלב 1: קרא את ה-DB מהר ותסגור את החיבור
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT path, mod_time FROM files")
        db_files = {row['path']: row['mod_time'] for row in c.fetchall()}

    # שלב 2: סרוק את הדיסק ללא DB פתוח
    flat_list = []
    found_paths = set()
    to_insert = []
    to_update = []

    for root_p in valid_roots:
        if not os.path.exists(root_p): continue
        for root, _, files in os.walk(root_p):
            for f in files:
                if not f.lower().endswith('.pdf'): continue
                full_p = os.path.join(root, f).replace('\\', '/')
                found_paths.add(full_p)
                try: mtime = os.path.getmtime(full_p)
                except OSError: mtime = 0
                if full_p not in db_files:
                    to_insert.append((full_p, f, mtime))
                elif db_files[full_p] != mtime:
                    to_update.append((mtime, full_p))
                flat_list.append({'n': f, 'p': full_p, 'd': os.path.basename(root)})

    # שלב 3: כתוב ל-DB בפעולה אחת קצרה
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            if to_insert: c.executemany("INSERT OR IGNORE INTO files (path, filename, mod_time) VALUES (?,?,?)", to_insert)
            if to_update: c.executemany("UPDATE files SET mod_time=?, scanned=0 WHERE path=?", to_update)
            to_delete = [(p,) for p in db_files if p not in found_paths]
            if to_delete: c.executemany("DELETE FROM files WHERE path=?", to_delete)
            conn.commit()
    except Exception as e:
        print(f"Disk Scan DB Write Error: {e}")

    return flat_list, valid_roots


def build_directory_tree(flat_list, valid_roots):
    tree = []
    for item in flat_list:
        parent_root = next((r for r in valid_roots if item['p'].startswith(r)), "")
        if parent_root:
            rel_path = os.path.relpath(item['p'], parent_root).replace('\\', '/')
        else:
            rel_path = item['n']

        parts = rel_path.split('/')
        curr_list = tree

        for part in parts[:-1]:
            found = next((c for c in curr_list if c.get('name') == part and c.get('type') == 'folder'), None)
            if not found:
                found = {'name': part, 'type': 'folder', 'children': []}
                curr_list.append(found)
            curr_list = found['children']

        curr_list.append({'name': item['n'], 'type': 'file', 'path': item['p']})

    return tree


def file_scan_worker():
    global scan_status
    scan_status.update({'is_scanning': True, 'done': False})
    try:
        flat_list, valid_roots = scan_files_on_disk(current_config['root_paths'])
        tree = build_directory_tree(flat_list, valid_roots)
        with open(TREE_FILE, 'w', encoding='utf-8') as f:
            json.dump({'tree': tree, 'flat': flat_list}, f)
    except Exception as e:
        print(f"Scan Worker Error: {e}")
    finally:
        scan_status.update({'done': True, 'is_scanning': False})


# ---------------- Indexing Logic (Multi-threaded) ----------------

def index_single_pdf(task):
    fid, path, fname = task
    if indexer_status.get('stop_flag'): return False
    try:
        fitz.TOOLS.mupdf_display_errors(False)
        batch = []
        with fitz.open(path) as doc:
            for page_num, page in enumerate(doc, start=1):
                text = page.get_text("text")
                if text and len(text.strip()) > 3:
                    clean = re.sub(r'\s+', ' ', text).strip()
                    batch.append((fid, page_num, clean))
        
        # כתיבת כל העמודים בבת אחת - הרבה יותר מהיר
        if batch:
            with get_db_connection() as conn:
                c = conn.cursor()
                c.execute("DELETE FROM pages WHERE file_id=?", (fid,))
                c.executemany("INSERT INTO pages VALUES (?,?,?)", batch)
                c.execute("UPDATE files SET scanned=1 WHERE id=?", (fid,))
                conn.commit()
        else:
            # גם אם אין טקסט, נסמן כסרוק כדי שלא ינסה שוב ושוב
            with get_db_connection() as conn:
                conn.execute("UPDATE files SET scanned=1 WHERE id=?", (fid,))
                conn.commit()
        return True
    except Exception:
        return False
        
def content_indexer_worker():
    global indexer_status
    indexer_status.update({'is_indexing': True, 'processed': 0, 'done': False, 'stop_flag': False})
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, path, filename FROM files WHERE scanned=0")
            tasks = [(t['id'], t['path'], t['filename']) for t in c.fetchall()]
        indexer_status['total_files'] = len(tasks)
        tasks.sort(key=lambda t: os.path.getsize(t[1]) if os.path.exists(t[1]) else 0)
        cpu_count = os.cpu_count() or 2
        max_workers = max(2, min(cpu_count - 1, 6))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(index_single_pdf, task): task for task in tasks}
            for future in as_completed(futures):
                if indexer_status['stop_flag']:
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                indexer_status['processed'] += 1
                task_info = futures[future]
                indexer_status['current'] = task_info[2]
                try: future.result()
                except: pass
    except Exception as e:
        print(f"Indexer Worker Error: {e}")
    finally:
        indexer_status.update({'done': True, 'is_indexing': False, 'current': ''})


def monitor_shutdown():
    start_time = time.time()
    while True:
        time.sleep(1)
        if time.time() - start_time < 15: continue
        if time.time() - server_status['last_heartbeat'] > 30: os._exit(0)

# ---------------- Flask Routes ----------------
@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico', mimetype='image/x-icon')


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/scan_start')
def scan_start():
    if not scan_status['is_scanning']:
        t = threading.Thread(target=file_scan_worker, daemon=True)
        t.daemon = True
        t.start()
    return jsonify({'status': 'started'})

@app.route('/scan_status')
def get_scan_status():
    with get_db_connection() as conn:
        c = conn.cursor()
        count = c.execute("SELECT COUNT(id) FROM files").fetchone()[0]
    scan_status['total_files'] = count
    return jsonify(scan_status)


@app.route('/index_start')
def index_start():
    if not indexer_status['is_indexing']:
        threading.Thread(target=content_indexer_worker, daemon=True).start()
    return jsonify({'status': 'started'})


@app.route('/index_stop')
def index_stop():
    indexer_status['stop_flag'] = True
    return jsonify({'status': 'stopping'})


@app.route('/index_status')
def get_index_status():
    return jsonify(indexer_status)

@app.route('/clear_index')
def clear_index():
    # סימון לאינדקסר לעצור כדי שלא ינעל את בסיס הנתונים
    indexer_status['stop_flag'] = True 
    
    def do_clear():
        time.sleep(0.5) # המתנה קלה לשחרור נעילות קיימות
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("DROP TABLE IF EXISTS pages")
            c.execute("CREATE VIRTUAL TABLE pages USING fts5(file_id UNINDEXED, page_num UNINDEXED, content)")
            c.execute("UPDATE files SET scanned=0")
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
            conn.close() # סגירת החיבור בסיום
        except Exception as e:
            print(f"Clear index error: {e}")
            
    threading.Thread(target=do_clear, daemon=True).start()
    return jsonify({'status': 'ok'})

@app.route('/search')
def search():
    q = request.args.get('q', '').strip()
    query = """SELECT f.path, f.filename, p.page_num, snippet(pages, 2, '<MARK>', '</MARK>', '...', 40) as snip
               FROM pages p JOIN files f ON p.file_id = f.id WHERE p.content MATCH ? LIMIT 500"""
    res = []
    try:
        q_safe = q.replace('"', '""')
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute(query, (f'"{q_safe}"',))
            rows = c.fetchall()
        for r in rows:
            res.append({
                'path': r['path'], 'name': r['filename'],
                'folder': os.path.basename(os.path.dirname(r['path'])),
                'page': r['page_num'], 'snippet': r['snip']
            })
    except: pass
    return jsonify(res)


@app.route('/fav_toggle', methods=['POST'])
def fav_toggle():
    path = (request.json or {}).get('path')
    if not path:  
        return jsonify({'status': 'error'}), 400
    added = False
    with get_db_connection() as conn:
        c = conn.cursor()
        try:
            c.execute("INSERT INTO favorites (path) VALUES (?)", (path,))
            added = True
        except sqlite3.IntegrityError:
            c.execute("DELETE FROM favorites WHERE path=?", (path,))
        conn.commit()
    return jsonify({'status': 'ok', 'added': added})


@app.route('/fav_list')
def fav_list():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT path FROM favorites")
        res = [{'path': r['path'], 'name': os.path.basename(r['path'])} for r in c.fetchall()]
    return jsonify(res)


@app.route('/history_log', methods=['POST'])
def history_log():
    path = request.json.get('path')
    if path:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO history (path, last_access) VALUES (?, ?)", (path, time.time()))
            conn.commit()
    return jsonify({'status': 'ok'})


@app.route('/get_history')
def get_history():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT path FROM history ORDER BY last_access DESC LIMIT 50")
        res = [{'path': r['path'], 'name': os.path.basename(r['path'])} for r in c.fetchall()]
    return jsonify(res)
    
    
@app.route('/history_remove', methods=['POST'])
def history_remove():
    path = (request.json or {}).get('path')
    if path:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM history WHERE path=?", (path,))
            conn.commit()
    return jsonify({'status': 'ok'})

@app.route('/pdf')
def get_pdf():
    path = request.args.get('path')
    q = request.args.get('q', '')
    if not path or not os.path.exists(path): return "File not found", 404
    if q:
        try:
            doc = fitz.open(path)
            for page in doc:
                for inst in page.search_for(q):
                    page.add_highlight_annot(inst)
            pdf_bytes = doc.tobytes(garbage=4, deflate=True)
            doc.close()
            return send_file(io.BytesIO(pdf_bytes), mimetype='application/pdf')
        except: pass
    return send_file(path)


@app.route('/preview_page')
def preview_page():
    path = request.args.get('path')
    page_num = int(request.args.get('page', 1))
    q = request.args.get('q', '')
    if not path or not os.path.exists(path): return "File not found", 404
    try:
        with fitz.open(path) as doc:
            with fitz.open() as new_doc:
                new_doc.insert_pdf(doc, from_page=page_num-1, to_page=page_num-1)
                if q:
                    for inst in new_doc[0].search_for(q):
                        new_doc[0].add_highlight_annot(inst)
                pdf_bytes = new_doc.tobytes(garbage=4, deflate=True)
            return send_file(io.BytesIO(pdf_bytes), mimetype='application/pdf')
    except: return "Error", 500

@app.route('/browse')
def browse():
    try:
        if getattr(sys, 'frozen', False):
            import tkinter as tk
            from tkinter import filedialog
            result = {'path': ''}
            def ask():
                root = tk.Tk()
                root.withdraw()
                root.attributes('-topmost', True)
                result['path'] = filedialog.askdirectory()
                root.destroy()
            t = threading.Thread(target=ask)
            t.start()
            t.join()
            if result['path']:
                return jsonify({'status': 'ok', 'path': result['path'].replace('\\', '/')})
        else:
            cmd = [sys.executable, '-c', "import tkinter as tk, sys; from tkinter import filedialog; root=tk.Tk(); root.withdraw(); root.attributes('-topmost', True); path=filedialog.askdirectory(); root.destroy(); sys.stdout.buffer.write(path.encode('utf-8'))"]
            kwargs = {}
            if os.name == 'nt': kwargs['creationflags'] = 0x08000000
            path = subprocess.check_output(cmd, **kwargs).decode('utf-8').strip()
            if path:
                return jsonify({'status': 'ok', 'path': path.replace('\\', '/')})
    except: pass
    return jsonify({'status': 'cancel'})

@app.route('/update_paths', methods=['POST'])
def update_paths():
    current_config['root_paths'] = request.json.get('paths', [])
    save_config()
    return jsonify({'status': 'ok'})


@app.route('/save_prefs', methods=['POST'])
def save_prefs():
    current_config['theme_mode'] = request.json.get('theme_mode', 0)
    save_config()
    return jsonify({'status': 'ok'})


@app.route('/get_tree')
def get_tree():
    if os.path.exists(TREE_FILE):
        try:
            with open(TREE_FILE, 'r', encoding='utf-8') as f:
                return jsonify(json.load(f))
        except: pass
    return jsonify({'tree': [], 'flat': []})


@app.route('/get_init')
def get_init():
    return jsonify({'paths': current_config['root_paths'], 'theme_mode': current_config['theme_mode']})


@app.route('/heartbeat')
def heartbeat():
    server_status['last_heartbeat'] = time.time()
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    init_db()
    load_config()
    threading.Thread(target=monitor_shutdown, daemon=True).start()

    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        browsers = ['chrome', 'google-chrome', 'msedge']
        browser_path = None
        for b in browsers:
            path = shutil.which(b)
            if path:
                browser_path = path
                break
        if not browser_path and os.name == 'nt':
            for p in [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                      r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]:
                if os.path.exists(p):
                    browser_path = p
                    break
        if browser_path:
            try:
                subprocess.Popen([browser_path, '--app=http://127.0.0.1:5001', '--start-maximized', '--window-position=0,0', '--window-size=1920,1080', '--disable-cache'])
            except:
                webbrowser.open("http://127.0.0.1:5001")
        else:
            webbrowser.open("http://127.0.0.1:5001")

    app.run(port=5001, debug=False, threaded=True)