from gevent import monkey
monkey.patch_all()
import gevent
import re
import os
import sys
import csv
import json
import time
import socket
import random
import string
import sqlite3
import threading
from datetime import datetime, timedelta
from io import StringIO, BytesIO
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
from flask_compress import Compress
from flask_socketio import SocketIO, emit, join_room, leave_room
# 1. 动态判断运行环境
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
    BUNDLE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    BUNDLE_DIR = BASE_DIR

# 2. 静态与模板目录配置
app = Flask(
    __name__,
    template_folder=os.path.join(BUNDLE_DIR, 'templates'),
    static_folder=os.path.join(BUNDLE_DIR, 'static')
)
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
SECRET_KEY_FILE = os.path.join(DATA_DIR, "secret.key")
if os.environ.get('SECRET_KEY'):
    app.secret_key = os.environ['SECRET_KEY']
else:
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, "rb") as f:
            app.secret_key = f.read()
    else:
        import secrets
        app.secret_key = secrets.token_bytes(32)
        with open(SECRET_KEY_FILE, "wb") as f:
            f.write(app.secret_key)

# 3. Session 安全 Cookie 与基础配置
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins="*")
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 604800

# 4. Gzip 压缩配置并激活
app.config['COMPRESS_MIMETYPES'] = [
    'text/html', 'text/css', 'text/xml', 
    'application/json', 'application/javascript'
]
app.config['COMPRESS_LEVEL'] = 6
app.config['COMPRESS_MIN_SIZE'] = 500

Compress(app)

# 全局内存缓存字典初始化
_cache_store = {}
def to_bool_str(val):
    if val is None:
        return '0'
    s = str(val).lower()
    return '1' if s in ['true', '1', 'yes', 'on'] else '0'

# 3. 数据库目录与文件路径配置
DB_FILE = os.path.join(DATA_DIR, "sports_data.db")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin888")
REFEREE_PASSWORD = os.environ.get("REFEREE_PASSWORD", "ref123")

# 补齐防并发穿透互斥锁与时间戳标记
_recalc_lock = threading.Lock()
_last_recalc_time = 0

# 4. 数据库连接基础函数（👈 补回此处缺失的函数）
def get_db_connection(readonly=False):
    conn = sqlite3.connect(DB_FILE, timeout=60) 
    conn.execute('PRAGMA journal_mode = WAL;')
    conn.execute('PRAGMA synchronous = NORMAL;')
    conn.execute('PRAGMA cache_size = -32000;')
    conn.execute('PRAGMA temp_store = MEMORY;')
    conn.execute('PRAGMA busy_timeout = 60000;')
    conn.row_factory = sqlite3.Row
    return conn

def parse_time_to_seconds(val):
    if not val or str(val).strip() == "": 
        return 0.0 
    try:
        s = str(val).strip().replace('：', ':').replace('。', '.')
        # 1. 优先处理冒号格式: 1:02.05 或 1:02:05
        if ':' in s:
            parts = s.split(':')
            if len(parts) == 2: 
                return int(parts[0]) * 60 + float(parts[1])
            elif len(parts) == 3: 
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])

        # 2. 处理裁判常用的小键盘双点格式: "1.02.05" -> 分.秒.毫秒
        if s.count('.') >= 2:
            parts = s.split('.')
            if len(parts) == 3:
                mins = int(parts[0])
                secs = int(parts[1])
                ms = float(f"0.{parts[2]}")
                return mins * 60 + secs + ms
            elif len(parts) == 4:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]) + float(f"0.{parts[3]}")

        # 3. 常规纯秒格式: "58.06"
        return float(s)
    except Exception:
        return 0.0
def parse_high_jump_tie_breaker(score_str, attempts_json):
    try:
        best_h = float(score_str)
    except Exception:
        return (0.0, 0, 0)
    
    if not attempts_json:
        return (best_h, -1, 0)
        
    try:
        records = json.loads(attempts_json) if isinstance(attempts_json, str) else attempts_json
        
    except Exception:
        return (best_h, -1, 0)
        
    height_keys = []
    for k in records.keys():
        try: height_keys.append((float(k), k))
        except Exception: pass
    height_keys.sort(key=lambda x: x[0])
    
    attempts_at_best = 1
    total_failures = 0
    
    for h_val, h_str in height_keys:
        if h_val <= best_h:
            attempts_list = records.get(h_str, [])
            if abs(h_val - best_h) < 0.001:
                if 'O' in attempts_list:
                    attempts_at_best = attempts_list.index('O') + 1
                else:
                    attempts_at_best = len(attempts_list) or 1
                    
            for att in attempts_list:
                if att == 'X':
                    total_failures += 1
                elif att == 'O' and abs(h_val - best_h) < 0.001:
                    break
                    
    return (best_h, -attempts_at_best, -total_failures)

def allocate_world_athletics_lanes(event_name, num_athletes=8, total_lanes=8):
    """
    依照世界田联 (World Athletics) 技术规则 TR 20.4 实施同档抽签排道：
    - 直道赛 (100m/跨栏): 1-4名抽[3,4,5,6], 5-6名抽[2,7], 7-8名抽[1,8]
    - 200米赛: 1-3名抽[5,6,7], 4-5名抽[4,8], 6名分3道, 7-8名抽[1,2]
    - 400米/各接力: 1-4名抽[4,5,6,7], 5-6名抽[3,8], 7-8名抽[1,2]
    返回排定好的道次列表，索引对应预赛名次 1~N。
    """
    clean = str(event_name).replace(" (预赛)", "").replace(" (决赛)", "").strip()
    is_straight = any(k in clean for k in ['100米', '100m', '110米', '110m', '60米', '50米', '栏']) and not any(k in clean for k in ['400', '4×', '4x', '接力'])
    is_200 = ('200' in clean or '300' in clean)

    assigned = [0] * num_athletes

    if total_lanes >= 8 or num_athletes >= 8:
        if is_straight:
            tier1 = [3, 4, 5, 6]
            tier2 = [2, 7]
            tier3 = [1, 8]
        elif is_200:
            tier1 = [5, 6, 7]
            tier2 = [4, 8]
            tier3 = [3, 1, 2] # 200米第6名优先分3道，7-8名抽1、2道
        else:
            tier1 = [4, 5, 6, 7]
            tier2 = [3, 8]
            tier3 = [1, 2]

        # Tier 1 抽签 (优质黄金道)
        t1_draw = tier1.copy()
        random.shuffle(t1_draw)
        for idx in range(min(len(t1_draw), num_athletes)):
            assigned[idx] = t1_draw[idx]

        # Tier 2 抽签
        t1_len = len(t1_draw)
        if num_athletes > t1_len:
            t2_draw = tier2.copy()
            random.shuffle(t2_draw)
            for idx in range(min(len(t2_draw), num_athletes - t1_len)):
                assigned[t1_len + idx] = t2_draw[idx]

        # Tier 3 抽签
        t2_len = t1_len + len(tier2)
        if num_athletes > t2_len:
            if is_200 and len(tier3) == 3:
                lane_6th = [tier3[0]]
                lanes_7_8 = tier3[1:].copy()
                random.shuffle(lanes_7_8)
                t3_final = lane_6th + lanes_7_8
            else:
                t3_final = tier3.copy()
                random.shuffle(t3_final)
            for idx in range(min(len(t3_final), num_athletes - t2_len)):
                assigned[t2_len + idx] = t3_final[idx]

    elif total_lanes == 6 or num_athletes == 6:
        # 6 道跑道自适应
        if is_straight:
            t1 = [3, 4]; random.shuffle(t1)
            t2 = [2, 5]; random.shuffle(t2)
            t3 = [1, 6]; random.shuffle(t3)
            assigned = (t1 + t2 + t3)[:num_athletes]
        else:
            t1 = [4, 5, 6]; random.shuffle(t1)
            t2 = [2, 3]; random.shuffle(t2)
            t3 = [1]
            assigned = (t1 + t2 + t3)[:num_athletes]
    else:
        # 4 道跑道自适应
        t1 = [2, 3]; random.shuffle(t1)
        t2 = [1, 4]; random.shuffle(t2)
        assigned = (t1 + t2)[:num_athletes]

    return assigned
# ========================================================
# 🌟 田赛远度 / 投掷多轮次成绩平分决胜算法（田径规程次优成绩判优）
# ========================================================
def parse_field_attempts_tie_breaker(score_str, attempts_json):
    """
    田径规则平分决胜：
    1. 比较最好成绩（第1好成绩）；
    2. 若相同，比较第二好成绩（次好成绩）；
    3. 若仍相同，依次比较第3、4、5、6好成绩；
    Python 元组 (tuple) 天然支持从前到后逐项比对大小。
    """
    try:
        best_val = float(str(score_str).replace('：', '.').replace(':', '.'))
    except Exception:
        best_val = 0.0

    scores = []
    if attempts_json:
        try:
            # 兼容字符串与列表两种解析格式
            att_list = json.loads(attempts_json) if isinstance(attempts_json, str) else attempts_json
            for val in att_list:
                try:
                    v = float(str(val).replace('：', '.').replace(':', '.'))
                    if v > 0:
                        scores.append(v)
                except Exception:
                    pass
        except Exception:
            pass
    scores.sort(reverse=True)
    if not scores and best_val > 0:
        scores.append(best_val)
    while len(scores) < 6:
        scores.append(0.0)
    return tuple(scores[:6])
def get_host_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        try: s.close()
        except Exception: pass
    return ip

# ========================================================
# 🌟 升级版高性能防击穿缓存（放在 app.py 约第 288 行）
# 特性：函数名、参数、返回值 100% 保持不变，已有接口一行都不用改！
# ========================================================
from gevent.lock import Semaphore

# 存储格式: cache_key -> {"data": 实际数据, "expire_at": 到期时间戳}
_cache_store = {}
# 锁字典: 每个 cache_key 独立一把协程锁，防止全站锁死
_key_locks = {}
_global_lock = Semaphore()

def _get_lock_for_key(key):
    """为每个缓存Key分配独立的协程锁"""
    with _global_lock:
        if key not in _key_locks:
            _key_locks[key] = Semaphore()
        return _key_locks[key]

def get_cached_data(cache_key, ttl_seconds=15):
    now = time.time()
    cached = _cache_store.get(cache_key)
    if cached and now < cached['expire_at']:
        return cached['data']
    return None

def set_cached_data(cache_key, data, ttl_seconds=15):
    _cache_store[cache_key] = {
        'data': data,
        'expire_at': time.time() + ttl_seconds
    }
def invalidate_event_cache(event_name, group_name=""):
    """
    精准失效受影响的项目和榜单缓存
    """
    if not _cache_store:
        return
    clean_core = re.sub(r"\(.*?\)|（.*?）|决赛|预赛", "", event_name).strip()
    
    keys_to_del = []
    for k in list(_cache_store.keys()):
        # 只要涉及到该项目（无论有没有带具体 group_name），一律清除缓存！
        if k.startswith("startlist_") and clean_core in k:
            keys_to_del.append(k)
        elif k.startswith("rank_"):
            keys_to_del.append(k)
            
    for k in keys_to_del:
        _cache_store.pop(k, None)
# 权限拦截器
def login_required(role_needed):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            is_api = request.path.startswith('/api/')
            
            if 'user_role' not in session:
                if is_api:
                    return jsonify({"status": "error", "msg": "未登录或登录已超时，请重新登录"}), 401
                if role_needed == 'admin': return redirect('/admin/login')
                elif role_needed == 'referee': return redirect('/referee/login')
                else: return redirect('/bm') 
            
            current_role = session['user_role']
            if role_needed == 'admin' and current_role != 'admin': 
                if is_api:
                    return jsonify({"status": "error", "msg": "权限不足：需要管理员权限"}), 403
                return redirect('/admin/login')
            if role_needed == 'referee' and current_role not in ['admin', 'referee']: 
                if is_api:
                    return jsonify({"status": "error", "msg": "权限不足：需要裁判权限"}), 403
                return redirect('/referee/login')
            if role_needed == 'team' and current_role not in ['admin', 'team']:
                if is_api:
                    return jsonify({"status": "error", "msg": "权限不足：需要领队权限"}), 403
                return redirect('/bm')

            return f(*args, **kwargs)
        return decorated_function
    return decorator
# 页面路由
@app.route('/team')
def team_login(): 
    return redirect('/bm')

@app.route('/admin/login')
def admin_login():
    return render_template('admin_login.html')

@app.route('/')
@app.route('/admin')
@login_required('admin')
def admin():
    local_ip = get_host_ip()
    return render_template('admin.html', 
                           local_ip=local_ip,
                           user_team_id=session.get('team_id', ''),
                           team_name=session.get('team_name', ''))

@app.route('/bm')
def bm_page():
    return render_template('bm.html', 
                           user_role=session.get('user_role'),
                           user_group_id=session.get('group_id'),
                           user_team_id=session.get('team_id'),
                           team_name=session.get('team_name'))

@app.route('/referee')
@app.route('/referee/login')
def referee():
    conn = get_db_connection()
    try:
        c = conn.cursor()
        groups = [dict(r) for r in c.execute("SELECT * FROM cfg_groups").fetchall()]
        teams = [dict(r) for r in c.execute("SELECT * FROM cfg_teams").fetchall()]
        events = [dict(r) for r in c.execute("SELECT * FROM cfg_events").fetchall()]
        return render_template('referee.html', groups=groups, teams_json=json.dumps(teams), events_json=json.dumps(events))
    finally:
        conn.close()
@app.route('/query')
def query_page():
    return render_template('query.html', title="成绩查询")
# 统一认证 API
@app.route('/api/check_session', methods=['GET'])
def check_session():
    role = session.get('user_role')
    if role:
        return jsonify({
            'status': 'success',
            'role': role,
            'team_id': session.get('team_id'),
            'team_name': session.get('team_name')
        })
    return jsonify({'status': 'fail', 'msg': '未登录'})
# ========================================================
# 🌟 登录防暴力破解限流器（兼容 Lucky 反代真实 IP）
# ========================================================
_login_failures = {}  # 格式: { ip: {"fails": [timestamp, ...], "lock_until": timestamp} }
_login_lock = threading.Lock()

def get_client_ip():
    # 优先从 Lucky / Nginx 传递的 X-Forwarded-For 提取真实访客 IP
    x_forwarded = request.headers.get('X-Forwarded-For')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.remote_addr or '127.0.0.1'

def check_ip_rate_limit(ip):
    now = time.time()
    with _login_lock:
        data = _login_failures.get(ip)
        if not data:
            return True, 0
        if data.get('lock_until', 0) > now:
            remaining = int(data['lock_until'] - now)
            return False, remaining
        # 清理 60 秒以外的过期失败记录
        data['fails'] = [t for t in data.get('fails', []) if now - t < 60]
        return True, 0

def record_ip_failure(ip):
    now = time.time()
    with _login_lock:
        if ip not in _login_failures:
            _login_failures[ip] = {"fails": [], "lock_until": 0}
        data = _login_failures[ip]
        data['fails'] = [t for t in data['fails'] if now - t < 60]
        data['fails'].append(now)
        # 1 分钟内累计 5 次失败，封禁 10 分钟 (600秒)
        if len(data['fails']) >= 5:
            data['lock_until'] = now + 600
            return True, 600
        return False, 5 - len(data['fails'])

def clear_ip_failure(ip):
    with _login_lock:
        _login_failures.pop(ip, None)
@app.route('/api/auth', methods=['POST'])
def api_auth():
    client_ip = get_client_ip()
    allowed, wait_seconds = check_ip_rate_limit(client_ip)
    if not allowed:
        minutes = max(1, wait_seconds // 60)
        return jsonify({
            'status': 'fail', 
            'msg': f'⚠️ 密码错误次数过多，IP已被限制登录！请等待 {minutes} 分钟后再试。'
        }), 429

    data = request.json or {}
    role_type = data.get('type')
    success = False
    
    if role_type == 'admin':
        username = data.get('username')
        password = data.get('password')
        if username == 'admin' and password == ADMIN_PASSWORD:
            session['user_role'] = 'admin'
            clear_ip_failure(client_ip)
            return jsonify({'status': 'success', 'redirect': '/admin'})
    elif role_type == 'referee':
        username = data.get('username')
        if username == 'referee' and data.get('password') == REFEREE_PASSWORD:
            session['user_role'] = 'referee'
            clear_ip_failure(client_ip)
            return jsonify({'status': 'success', 'redirect': '/referee'})
    elif role_type == 'team':
        username = data.get('username')
        password = data.get('password')
        conn = get_db_connection()
        try:
            c = conn.cursor()
            auth_row = c.execute("SELECT password FROM team_auth WHERE team_name = ?", (username,)).fetchone()
            if auth_row and str(auth_row['password']) == str(password):
                team_row = c.execute("SELECT id, group_id, name FROM cfg_teams WHERE name = ?", (username,)).fetchone()
                if team_row:
                    session['user_role'] = 'team'
                    session['team_id'] = team_row['id']      
                    session['group_id'] = team_row['group_id']
                    session['team_name'] = team_row['name']
                    clear_ip_failure(client_ip)
                    return jsonify({'status': 'success', 'redirect': '/bm'})
        finally:
            conn.close()

    # 登录失败，累计计数
    is_locked, info = record_ip_failure(client_ip)
    if is_locked:
        return jsonify({'status': 'fail', 'msg': '⚠️ 密码连续输错 5 次，该 IP 已被锁定 10 分钟！'}), 429
    return jsonify({'status': 'fail', 'msg': f'认证失败：登录名或密码错误（还可尝试 {info} 次）'})
@app.route('/api/logout')
def logout():
    from_role = request.args.get('from', '')
    role = session.get('user_role')
    session.clear()
    
    if from_role == 'team':
        return redirect('/bm')
    if from_role == 'referee':
        return redirect('/referee/login')
    if from_role == 'admin':
        return redirect('/admin/login')

    if role == 'admin':
        return redirect('/admin/login')
    elif role == 'referee':
        return redirect('/referee/login')
    return redirect('/bm')

# 检录与赛程监控 API
@app.route('/api/toggle_checkin', methods=['POST'])
@login_required('referee')
def toggle_checkin():
    data = request.json or {}
    start_id = data.get('id')
    status = int(data.get('checked_in', 0))
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        
        start_row = c.execute("SELECT id, name, team_name, group_name, event_name, gender FROM start_list WHERE id = ?", (start_id,)).fetchone()
       
        if not start_row and data.get('name'):
            start_row = c.execute("""
                SELECT id, name, team_name, group_name, event_name, gender 
                FROM start_list 
                WHERE name = ? AND group_name = ? AND event_name = ?
                LIMIT 1
            """, (data.get('name'), data.get('group_name'), data.get('event_name'))).fetchone()
            if start_row:
                start_id = start_row['id']

        if not start_row:
            conn.close()
            return jsonify({"status": "error", "msg": "未找到对应的道次记录"})
        c.execute("UPDATE start_list SET checked_in = ? WHERE id = ?", (status, start_id))
        
        cur_event_name = start_row['event_name']
        clean_core_evt = re.sub(r"\(.*?\)|（.*?）|预赛|决赛", "", cur_event_name).strip()

        if status == 2:
            c.execute("""
                UPDATE registrations 
                SET score = '弃权' 
                WHERE (name = ? OR ? LIKE '%' || name || '%')
                  AND team_name = ? AND group_name = ? 
                  AND (event_name = ? OR event_name = ? OR event_name LIKE ? || '%')
                  AND (score = '' OR score IS NULL OR score = '弃权')
            """, (start_row['name'], start_row['name'], start_row['team_name'], start_row['group_name'], cur_event_name, clean_core_evt, clean_core_evt))
        else:
            c.execute("""
                UPDATE registrations 
                SET score = '' 
                WHERE (name = ? OR ? LIKE '%' || name || '%')
                  AND team_name = ? AND group_name = ? 
                  AND (event_name = ? OR event_name = ? OR event_name LIKE ? || '%')
                  AND score = '弃权'
            """, (start_row['name'], start_row['name'], start_row['team_name'], start_row['group_name'], cur_event_name, clean_core_evt, clean_core_evt))

        conn.commit()
        if '_cache_store' in globals():
            _cache_store.pop("get_data_public", None)

        # 广播检录状态更新，同步多名检录裁判终端
        socketio.emit('checkin_updated', {'id': start_id, 'status': status})
        return jsonify({"status": "success", "checked_in": status})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
@app.route('/api/get_monitor_progress')
def get_monitor_progress():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        # 读取趣味项目与田赛配置字典
        evt_cfg_rows = c.execute("SELECT name, type, is_fun FROM cfg_events").fetchall()
        evt_cfg_map = {}
        for r in evt_cfg_rows:
            e_name = r['name']
            e_type = str(r['type'] or '')
            is_fun = (str(r['is_fun']) == '1') or ('趣味' in e_type)
            evt_cfg_map[e_name] = {'type': e_type, 'is_fun': is_fun}

        sql = """
            SELECT 
                s.id, s.group_name, s.event_name, s.gender, s.heat, s.lane, s.bib, s.name, s.team_name, 
                s.est_time, s.time_index, s.is_field,
                IFNULL(s.is_started, 0) as is_started,
                IFNULL(s.checked_in, 0) as checked_in,
                CASE 
                    WHEN s.score IS NOT NULL AND TRIM(s.score) != '' THEN s.score
                    ELSE IFNULL(r.score, '')
                END AS score
            FROM start_list s
            LEFT JOIN registrations r 
                ON s.team_name = r.team_name 
                AND s.group_name = r.group_name 
                AND s.gender = r.gender
                AND (
                    -- 如果道次本身是决赛，必须严格匹配决赛成绩，禁止跨赛次关联预赛成绩
                    (s.event_name LIKE '%决赛%' AND r.event_name = s.event_name)
                    OR
                    -- 非决赛项目才允许模糊匹配纯项目名
                    (s.event_name NOT LIKE '%决赛%' AND (
                        r.event_name = s.event_name 
                        OR r.event_name = REPLACE(s.event_name, ' (预赛)', '')
                    ))
                )
                AND (
                    (s.event_name LIKE '%4x%' OR s.event_name LIKE '%4×%' OR s.event_name LIKE '%接力%')
                    OR s.name = r.name
                )
            GROUP BY s.id
            ORDER BY s.time_index ASC, CAST(s.heat AS INTEGER) ASC, CAST(s.lane AS INTEGER) ASC
        """
        rows = c.execute(sql).fetchall()
        
        tasks_map = {}
        for r in rows:
            key = f"{r['group_name']}#{r['event_name']}#{r['gender']}#{r['heat']}"
            if key not in tasks_map:
                e_name = r['event_name']
                # 判定大类：fun (趣味) > field (田赛) > track (径赛)
                cfg_info = evt_cfg_map.get(e_name, {})
                clean_name = re.sub(r'[\(（].*?[\)）]', '', e_name).strip()
                if not cfg_info:
                    cfg_info = evt_cfg_map.get(clean_name, {})

                is_fun = cfg_info.get('is_fun') or any(k in e_name for k in ['趣味', '足球', '篮球', '投篮', '蛙跳', '跳绳', '拔河', '毛毛虫', '沙包', '毽'])
                is_field = (r['is_field'] == 1) or any(k in e_name for k in ['跳高', '跳远', '铅球', '实心球', '标枪', '铁饼', '三级跳', '立定跳远'])

                if is_fun:
                    category = 'fun'
                elif is_field:
                    category = 'field'
                else:
                    category = 'track'

                tasks_map[key] = {
                    "group_name": r['group_name'],
                    "event_name": r['event_name'],
                    "gender": r['gender'],
                    "heat": r['heat'],
                    "est_time": r['est_time'] or '',
                    "time_index": int(r['time_index'] or 0),
                    "is_started": r['is_started'],
                    "category": category,
                    "athletes": []
                }
            tasks_map[key]["athletes"].append({
                "id": r['id'],
                "name": r['name'],
                "checked_in": r['checked_in'],
                "score": r['score']
            })

        tasks = []
        for key, t in tasks_map.items():
            total = len(t["athletes"])
            
            def is_athlete_finished(a):
                s = str(a.get("score") or "").strip()
                c = int(a.get("checked_in") or 0)
                if c == 2: return True
                if any(k in s for k in ['弃权', 'DNS', 'DNF', 'DQ']): return True
                if s != '' and s != '-': return True
                return False

            finished_count = sum(1 for a in t["athletes"] if is_athlete_finished(a))
            scored_count = sum(1 for a in t["athletes"] if a.get("score") and not any(k in str(a["score"]) for k in ['弃权', 'DNS', 'DNF', 'DQ', '-']))
            checked_count = sum(1 for a in t["athletes"] if int(a.get("checked_in") or 0) == 1)

            if total > 0 and finished_count >= total:
                status = "finished"
            elif t["is_started"] == 1 or finished_count > 0:
                status = "ongoing"
            elif checked_count > 0:
                status = "pending"
            else:
                status = "unchecked"

            t["status"] = status
            t["total_count"] = total
            t["checked_count"] = checked_count
            t["scored_count"] = scored_count
            tasks.append(t)

        return jsonify({"status": "success", "tasks": tasks})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
# 业务功能 API
# -------------------- 👇 替换原来的 recalculate_all_points 👇 --------------------
def do_recalculate():
    global _last_recalc_time
    if '_recalc_lock' in globals():
        if not _recalc_lock.acquire(blocking=True, timeout=5):
            return "后台正在核算积分中，请稍候"

    conn = get_db_connection()
    c = conn.cursor()
    count = 0
    try:
        updates_individual = []
        updates_team_reset = []

        groups_genders = c.execute("SELECT DISTINCT group_name, gender FROM start_list WHERE group_name != ''").fetchall()
        all_cfgs = {row['name']: dict(row) for row in c.execute("SELECT * FROM cfg_events").fetchall()}

        group_qualify_map = {}
        try:
            gq_row = c.execute("SELECT value FROM sys_config WHERE key = 'group_qualify_map'").fetchone()
            if gq_row and gq_row['value']:
                group_qualify_map = json.loads(gq_row['value'])
        except Exception:
            pass

        for gg in groups_genders:
            g_name, gender = gg['group_name'], gg['gender']

            group_records_raw = []
            try: 
                group_records_raw = c.execute("SELECT event_name, gender, records_json FROM cfg_group_records WHERE group_name = ?", (g_name,)).fetchall()
            except Exception: 
                pass
            group_records_map = {}
            for gr in group_records_raw:
                group_records_map[f"{gr['event_name']}_{gr['gender']}"] = json.loads(gr['records_json'])

            # 🌟 修复 1：同时从 start_list 和 registrations 提取出有成绩的项目（解决接力成绩只存在于 start_list 的问题）
            distinct_events_rows = c.execute("""
                SELECT DISTINCT event_name FROM start_list 
                WHERE group_name = ? AND gender = ? AND score != '' AND score IS NOT NULL
                UNION
                SELECT DISTINCT event_name FROM registrations 
                WHERE group_name = ? AND gender = ? AND score != '' AND score IS NOT NULL
            """, (g_name, gender, g_name, gender)).fetchall()
            
            distinct_events = [r[0] for r in distinct_events_rows if r[0]]
            if not distinct_events: 
                continue

            event_map = {}
            for evt in distinct_events:
                core = re.sub(r"\(.*?\)|（.*?）|决赛|预赛|及格赛|男子|女子|混合|男|女|第一组|第二组|第三组|第四组|第\d+组|场地\d+", "", evt).strip()
                if not core: core = evt
                if core not in event_map: 
                    event_map[core] = []
                event_map[core].append(evt)

            for core_name, sub_events in event_map.items():
                norm_core = core_name.replace('*', '×').replace('x', '×').replace('X', '×')
                cfg = all_cfgs.get(core_name) or all_cfgs.get(norm_core)
                if not cfg:
                    prefix = "女子" if gender == "女" else "男子"
                    cfg = all_cfgs.get(prefix + core_name) or all_cfgs.get(prefix + norm_core)
                if not cfg: 
                    for k, v in all_cfgs.items():
                        clean_k = k.replace('*', '×').replace('x', '×')
                        if clean_k in norm_core or norm_core in clean_k: 
                            cfg = v
                            break
                
                has_prelim = False
                if cfg:
                    has_prelim = (to_bool_str(cfg.get('has_prelim') or cfg.get('hasPrelim')) == '1')

                is_team_event = False
                if cfg:
                    is_team_event = (to_bool_str(cfg.get('is_team') or cfg.get('isTeam')) == '1') or (to_bool_str(cfg.get('is_relay') or cfg.get('isRelay')) == '1')
                if not is_team_event:
                    is_team_event = (re.search(r'4[xX*×]|接力|集体|团体|迎面', norm_core) is not None)

                # 判定成绩优劣方向：远度/高度/投掷/计数类降序；跑步/计时类升序
                is_field_or_count = any(k in norm_core for k in ['跳远', '跳高', '实心球', '铅球', '投掷', '掷', '球', '跳绳', '投篮', '起坐', '引体', '垫球', '踢毽'])
                if cfg and ('田' in str(cfg.get('type') or '') or '健康' in str(cfg.get('type') or '')):
                    if not any(k in norm_core for k in ['跑', '绕杆', '计时', '50米', '100米']):
                        is_field_or_count = True

                # 确定需要计分的最终赛次（有预决赛的只对决赛计分；无预决赛的对直决计分）
                target_events = []
                if has_prelim:
                    for sub_evt in sub_events:
                        if '决赛' in sub_evt: target_events.append(sub_evt)
                else:
                    for sub_evt in sub_events:
                        if '预赛' not in sub_evt: target_events.append(sub_evt)

                if not target_events: 
                    target_events = sub_events

                target_placeholders = ','.join(['?'] * len(target_events))

                # ========================================================
                # 🌟 核心拦截规则：严格检查该组别、该性别、该项目是否【全员完赛】
                # 条件：凡是非待定、非占位的实际参赛选手，必须 (有成绩 或 checked_in==2弃权)
                # ========================================================
                
                # 1. 检查 start_list 中是否有未完赛选手
                uncompleted_start = c.execute(f"""
                    SELECT COUNT(*) 
                    FROM start_list 
                    WHERE group_name = ? AND gender = ? 
                      AND event_name IN ({target_placeholders})
                      AND (score IS NULL OR TRIM(score) = '' OR score = '-')
                      AND checked_in != 2
                      AND name != '' AND name IS NOT NULL
                      AND name NOT LIKE '%待定%'
                      AND team_name != '预赛出线'
                """, [g_name, gender] + target_events).fetchone()[0]

                # 2. 检查 registrations 中是否有未完赛选手（主要针对立定跳远、跳绳等体测项）
                uncompleted_reg = c.execute(f"""
                    SELECT COUNT(*) 
                    FROM registrations 
                    WHERE group_name = ? AND gender = ? 
                      AND event_name IN ({target_placeholders})
                      AND (score IS NULL OR TRIM(score) = '' OR score = '-')
                      AND (score NOT LIKE '%弃权%' AND score NOT LIKE '%DNS%')
                      AND name != '' AND name IS NOT NULL
                """, [g_name, gender] + target_events).fetchone()[0]

                # 只要存在任何一个既没有录入成绩、也没有标记弃权的选手，一律判定为“比赛尚未全部完赛”！坚决跳过不计算！
                # 同时清空可能残留的旧积分，防止显示半路积分
                if uncompleted_start > 0 or uncompleted_reg > 0:
                    c.execute(f"""
                        UPDATE registrations 
                        SET points = 0, rank = '' 
                        WHERE group_name = ? AND gender = ? AND event_name IN ({target_placeholders})
                    """, [g_name, gender] + target_events)
                    c.execute(f"""
                        UPDATE start_list 
                        SET points = 0 
                        WHERE group_name = ? AND gender = ? AND event_name IN ({target_placeholders})
                    """, [g_name, gender] + target_events)
                    continue

                # ========================================================
                # 🌟 全员完赛校验通过：开始全场统一汇总成绩、排名与赋分
                # ========================================================
                placeholders = ','.join(['?'] * len(target_events))
                
                all_start_rows = c.execute(f"""
                    SELECT id, name, team_name, event_name, score, attempts_json, 'start' as source
                    FROM start_list 
                    WHERE group_name=? AND gender=? AND event_name IN ({placeholders}) 
                      AND score != '' AND score IS NOT NULL AND score != '-'
                      AND score NOT LIKE '%弃权%' AND score NOT LIKE '%DNS%' AND score NOT LIKE '%DQ%'
                      AND name NOT LIKE '%待定%' AND team_name != '预赛出线'
                """, [g_name, gender] + target_events).fetchall()

                all_reg_rows = c.execute(f"""
                    SELECT id, name, team_name, event_name, score, attempts_json, 'reg' as source
                    FROM registrations 
                    WHERE group_name=? AND gender=? AND event_name IN ({placeholders}) 
                      AND score != '' AND score IS NOT NULL AND score != '-'
                      AND score NOT LIKE '%弃权%' AND score NOT LIKE '%DNS%' AND score NOT LIKE '%DQ%'
                """, [g_name, gender] + target_events).fetchall()

                combined_rows = [dict(r) for r in all_start_rows] + [dict(r) for r in all_reg_rows]
                if not combined_rows:
                    continue

                # 避免个人兼项或双表关联重复，按队伍或个人合并最好成绩
                unique_entries = {}
                for item in combined_rows:
                    key = f"TEAM_{item['team_name']}" if is_team_event else f"ATH_{item['team_name']}_{item['name']}"
                    item['_val'] = parse_time_to_seconds(item['score'])
                    if item['_val'] <= 0:
                        continue
                    
                    if key not in unique_entries:
                        unique_entries[key] = item
                    else:
                        if is_field_or_count:
                            if item['_val'] > unique_entries[key]['_val']:
                                unique_entries[key] = item
                        else:
                            if item['_val'] < unique_entries[key]['_val']:
                                unique_entries[key] = item

                final_list = [item for item in unique_entries.values() if item['_val'] > 0]
                if not final_list:
                    continue

                # 全场总排序
                final_list.sort(key=lambda x: x['_val'], reverse=is_field_or_count)

                # 录取名次与名次分规则
                score_rule = cfg.get('score_rule', "9,7,6,5,4,3,2,1") if cfg else "9,7,6,5,4,3,2,1"
                rules = [int(x) for x in score_rule.replace('，', ',').split(',') if x.strip().isdigit()]
                if not rules: rules = [9, 7, 6, 5, 4, 3, 2, 1]
                
                cfg_double = (to_bool_str(cfg.get('is_double_score')) == '1') if cfg else False
                is_double = is_team_event or cfg_double
                qualify_limit = 8
                if cfg:
                    raw_qc = cfg.get('qualify_count') or cfg.get('qualifyCount')
                    if raw_qc is not None and str(raw_qc).isdigit():
                        qualify_limit = int(raw_qc)
                if f"{g_name}_{norm_core}" in group_qualify_map:
                    qualify_limit = int(group_qualify_map[f"{g_name}_{norm_core}"])

                # 计算全场名次（处理并列）
                current_rank = 1
                for i, item in enumerate(final_list):
                    if i > 0:
                        prev_item = final_list[i - 1]
                        if abs(item['_val'] - prev_item['_val']) >= 0.0001:
                            current_rank = i + 1
                    item['computed_rank'] = current_rank

                for item in final_list:
                    rnk = item['computed_rank']
                    p = 0
                    if rnk <= len(rules) and rnk <= qualify_limit:
                        p = rules[rnk - 1]
                        if is_double:
                            p *= 2

                    if is_team_event:
                        updates_team_reset.append((p, 0, str(rnk), item['team_name'], g_name, gender, item['event_name'], item['id']))
                    else:
                        updates_individual.append((p, 0, str(rnk), item['name'], item['team_name'], g_name, gender, item['event_name']))
                count += 1
        
        # 🌟 极速批量落盘
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE registrations SET points = 0, record_bonus = 0 WHERE event_name LIKE '%接力%' OR event_name LIKE '%4x%' OR event_name LIKE '%4×%'")
        c.execute("UPDATE start_list SET points = 0 WHERE event_name LIKE '%接力%' OR event_name LIKE '%4x%' OR event_name LIKE '%4×%'")

        if updates_team_reset:
            # 1. 直接更新 start_list 本身对应道次的 points！确保前端直接读到！
            c.executemany("UPDATE start_list SET points = ? WHERE id = ?", [(u[0], u[7]) for u in updates_team_reset])
            
            # 2. 同时更新 registrations，使该班级跑接力的所有队员都获得积分和名次
            c.executemany("""
                UPDATE registrations 
                SET points = ?, record_bonus = ?, rank = ? 
                WHERE team_name = ? AND group_name = ? AND gender = ? 
                  AND (event_name = ? OR event_name = ? || ' (预赛)' OR event_name = REPLACE(?, ' (预赛)', '') OR event_name LIKE '%4%100%')
            """, [(u[0], u[1], u[2], u[3], u[4], u[5], u[6], u[6], u[6]) for u in updates_team_reset])

        if updates_individual:
            # 🌟 精确通过 (姓名 + 队伍名 + 组别 + 性别 + 项目名) 将名次与积分写入 registrations 表
            c.executemany("""
                UPDATE registrations 
                SET points = ?, record_bonus = ?, rank = ? 
                WHERE name = ? AND team_name = ? AND group_name = ? AND gender = ?
                  AND (event_name = ? OR event_name = ? || ' (预赛)' OR event_name = REPLACE(?, ' (预赛)', '') OR event_name LIKE ? || '%')
            """, [(u[0], u[1], u[2], u[3], u[4], u[5], u[6], u[7], u[7], u[7], u[7]) for u in updates_individual])

            # 同时回写 start_list 表（若存在对应道次记录）
            c.executemany("""
                UPDATE start_list 
                SET points = ? 
                WHERE name = ? AND team_name = ? AND group_name = ? AND gender = ?
                  AND (event_name = ? OR event_name LIKE ? || '%')
            """, [(u[0], u[3], u[4], u[5], u[6], u[7], u[7]) for u in updates_individual])

        conn.commit()
        _last_recalc_time = time.time()
        if '_cache_store' in globals():
            _cache_store.clear()

        return f'计算完毕！已处理 {count} 个项目。'
    except Exception as e:
        if 'conn' in locals(): conn.rollback()
        return str(e)
    finally:
        if 'conn' in locals(): conn.close()
        if '_recalc_lock' in globals():
            try: _recalc_lock.release()
            except Exception: pass

@app.route('/api/recalculate_all_points', methods=['POST'])
@login_required('admin')
def api_recalculate_all_points():
    msg = do_recalculate()
    if '_cache_store' in globals():
        _cache_store.clear()
    return jsonify({'status': 'success', 'msg': msg})
@app.route('/api/delete_athlete', methods=['POST'])
def delete_athlete():
    current_role = session.get('user_role')
    if current_role not in ['admin', 'team']:
        return jsonify({"status": "error", "msg": "未授权"}), 401
    
    data = request.json or {}
    name = data.get('name')
    team_id = session.get('team_id') if current_role == 'team' else data.get('team_id')

    if not name or not team_id:
        return jsonify({"status": "error", "msg": "参数不完整"}), 400

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        # 1. 查询对应队伍名
        t_row = c.execute("SELECT name FROM cfg_teams WHERE id = ?", (str(team_id),)).fetchone()
        team_name = t_row['name'] if t_row else ''

        # 2. 删除报名表数据
        c.execute("DELETE FROM registrations WHERE name = ? AND team_id = ?", (name, str(team_id)))
        
        # 3. 联动清理道次编排表，防止形成孤岛数据
        if team_name:
            c.execute("DELETE FROM start_list WHERE name = ? AND team_name = ?", (name, team_name))
        
        conn.commit()
        _cache_store.clear()
        return jsonify({"status": "success", "msg": "删除成功，道次与报名已同步清理"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)}), 500
    finally:
        conn.close()
    
@app.route('/api/clear_all_athletes', methods=['POST'])
@login_required('admin')
def clear_all_athletes():
    data = request.json or {}
    team_id = data.get('team_id')
    group_id = data.get('group_id')

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        if team_id:
            t_row = c.execute("SELECT name FROM cfg_teams WHERE id = ?", (str(team_id),)).fetchone()
            t_name = t_row[0] if t_row else ''
            c.execute("DELETE FROM registrations WHERE team_id = ?", (str(team_id),))
            if t_name:
                c.execute("DELETE FROM start_list WHERE team_name = ?", (t_name,))
            msg = "已成功清空当前代表队的全部报名运动员及编排记录！"
        elif group_id:
            g_row = c.execute("SELECT name FROM cfg_groups WHERE id = ?", (str(group_id),)).fetchone()
            g_name = g_row[0] if g_row else ''
            c.execute("DELETE FROM registrations WHERE group_id = ?", (str(group_id),))
            if g_name:
                c.execute("DELETE FROM start_list WHERE group_name = ?", (g_name,))
            msg = "已成功清空当前组别的全部报名运动员及编排记录！"
        else:
            c.execute("DELETE FROM registrations")
            c.execute("DELETE FROM start_list")
            c.execute("DELETE FROM sys_config WHERE key = 'manual_schedule_map'")
            c.execute("DELETE FROM system_settings WHERE key = 'active_track_heat'")
            msg = "已成功清空全场所有报名运动员及赛程道次编排！"

        conn.commit()
        if '_cache_store' in globals():
            _cache_store.clear()
        return jsonify({"status": "success", "msg": msg})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)}), 500
    finally:
        conn.close()
@app.route('/api/update_point', methods=['POST'])
@login_required('admin')
def update_point():
    data = request.json or {}
    conn = get_db_connection()
    try:
        conn.execute("UPDATE registrations SET points = ? WHERE id = ?", (data['points'], data['id']))
        conn.commit()
        return jsonify({'status': 'success', 'msg': '积分修改成功'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})
    finally:
        conn.close()

@app.route('/api/calculate_team_ranking', methods=['POST'])
def calculate_team_ranking():
    """
    查询端纯只读展示接口：
    不执行任何计算与重算逻辑，直接聚合管理端已核算落盘好的 points, rank, record_bonus
    """
    data = request.json or {}
    g_name = data.get('group_name')
    if not g_name:
        return jsonify({"columns": [], "teams": []})

    cache_key = f"rank_read_{g_name}"
    cached = get_cached_data(cache_key, ttl_seconds=60)
    if cached is not None:
        return jsonify(cached)

    conn = get_db_connection()
    c = conn.cursor()
    try:
        # 1. 读取该组别下所有代表队基础底表及自定义加分项
        team_rows = c.execute("""
            SELECT t.name, t.custom_scores 
            FROM cfg_teams t
            LEFT JOIN cfg_groups g ON t.group_id = g.id
            WHERE g.name = ?
            ORDER BY IFNULL(t.sort_order, t.rowid) ASC
        """, (g_name,)).fetchall()

        col_row = c.execute("SELECT value FROM sys_config WHERE key = 'custom_score_cols'").fetchone()
        custom_cols = []
        if col_row and col_row['value']:
            try: custom_cols = json.loads(col_row['value'])
            except Exception: custom_cols = []

        team_dict = {}
        for tr in team_rows:
            s_map = {}
            if tr['custom_scores']:
                try: s_map = json.loads(tr['custom_scores'])
                except Exception: s_map = {}
            team_dict[tr['name']] = {
                "name": tr['name'],
                "score": 0.0,
                "total_record_bonus": 0,
                "gold": 0,
                "silver": 0,
                "bronze": 0,
                "custom_scores": s_map
            }

        # 2. 直接读取 registrations 中管理端已存好的分值与名次（接力队按项目去重）
        sql = """
            WITH clean_scores AS (
                SELECT team_name, points, record_bonus, rank,
                       ROW_NUMBER() OVER (
                           PARTITION BY 
                               CASE 
                                   WHEN (event_name LIKE '%4x%' OR event_name LIKE '%4×%' OR event_name LIKE '%接力%' OR event_name LIKE '%集体%') 
                                   THEN team_name || '#' || event_name || '#' || gender
                                   ELSE id
                               END 
                           ORDER BY points DESC
                       ) as rn
                FROM registrations
                WHERE group_name = ? AND points > 0
            )
            SELECT 
                team_name,
                SUM(points) as total_points,
                SUM(record_bonus) as total_bonus,
                SUM(CASE WHEN rank = '1' THEN 1 ELSE 0 END) as gold,
                SUM(CASE WHEN rank = '2' THEN 1 ELSE 0 END) as silver,
                SUM(CASE WHEN rank = '3' THEN 1 ELSE 0 END) as bronze
            FROM clean_scores
            WHERE rn = 1
            GROUP BY team_name
        """
        rows = c.execute(sql, (g_name,)).fetchall()

        for r in rows:
            t_name = r['team_name']
            if t_name in team_dict:
                team_dict[t_name]['score'] = float(r['total_points'] or 0)
                team_dict[t_name]['total_record_bonus'] = int(r['total_bonus'] or 0)
                team_dict[t_name]['gold'] = int(r['gold'] or 0)
                team_dict[t_name]['silver'] = int(r['silver'] or 0)
                team_dict[t_name]['bronze'] = int(r['bronze'] or 0)

        # 3. 计入班级自定义附加分（如跑操），并排序
        final_list = []
        for t_name, item in team_dict.items():
            extra_sum = sum(float(item['custom_scores'].get(col, 0) or 0) for col in custom_cols)
            item['score'] = round(item['score'] + extra_sum, 2)
            final_list.append(item)

        final_list.sort(key=lambda x: (x['score'], x['gold'], x['silver'], x['bronze']), reverse=True)

        result_payload = {
            "columns": custom_cols,
            "teams": final_list
        }
        set_cached_data(cache_key, result_payload, ttl_seconds=60)
        return jsonify(result_payload)
    except Exception as e:
        print("读取团体总分异常:", e)
        return jsonify({"columns": [], "teams": []})
    finally:
        conn.close()

@app.route('/api/save_competition_date', methods=['POST'])
@login_required('admin')
def save_competition_date():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        data = request.json or {}
        start_date = data.get('start_date', '')
        end_date = data.get('end_date', '')
        
        c.execute("""CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)""")
        c.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('start_date', ?)", (start_date,))
        c.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('end_date', ?)", (end_date,))
        
        # 🌟 同步双写进 sys_config，保证全量配置导出时 100% 具备此字段
        c.execute("""CREATE TABLE IF NOT EXISTS sys_config (key TEXT PRIMARY KEY, value TEXT)""")
        c.execute("INSERT OR REPLACE INTO sys_config (key, value) VALUES ('start_date', ?)", (start_date,))
        c.execute("INSERT OR REPLACE INTO sys_config (key, value) VALUES ('end_date', ?)", (end_date,))
        
        conn.commit()
        return jsonify({"success": True, "message": "比赛起止时间配置成功！"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        conn.close()

@app.route('/api/get_competition_date', methods=['GET'])
def get_competition_date():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)")
        res_start = c.execute("SELECT value FROM system_settings WHERE key='start_date'").fetchone()
        res_end = c.execute("SELECT value FROM system_settings WHERE key='end_date'").fetchone()
        
        # 兜底：若 system_settings 缺失，去 sys_config 补查
        s_date = res_start[0] if (res_start and res_start[0]) else ""
        e_date = res_end[0] if (res_end and res_end[0]) else ""
        if not s_date or not e_date:
            c.execute("CREATE TABLE IF NOT EXISTS sys_config (key TEXT PRIMARY KEY, value TEXT)")
            cfg_s = c.execute("SELECT value FROM sys_config WHERE key='start_date'").fetchone()
            cfg_e = c.execute("SELECT value FROM sys_config WHERE key='end_date'").fetchone()
            if not s_date and cfg_s: s_date = cfg_s[0]
            if not e_date and cfg_e: e_date = cfg_e[0]

        return jsonify({
            "success": True, 
            "start_date": s_date,
            "end_date": e_date
        })
    except Exception:
        return jsonify({"success": False, "start_date": "", "end_date": ""})
    finally:
        conn.close()
# 起点裁判与终点裁判联动 API
@app.route('/api/push_active_heat', methods=['POST'])
@login_required('referee')
def push_active_heat():
    data = request.json or {}
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        g_name = data.get('group_name', '')
        e_name = data.get('event_name', '')
        gender = data.get('gender', '')
        heat = str(data.get('heat', '1'))

        c.execute("""CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)""")
        active_row = c.execute("SELECT value FROM system_settings WHERE key='active_track_heat'").fetchone()
        
        force_repush = data.get('force', False)
        clean_gender = '女' if '女' in gender else ('男' if '男' in gender else gender)

        if active_row and active_row[0] and not force_repush:
            try:
                cur_active = json.loads(active_row[0])
                is_same_heat = (
                    cur_active.get('group_name') == g_name and 
                    cur_active.get('event_name') == e_name and 
                    cur_active.get('gender') == clean_gender and 
                    str(cur_active.get('heat')) == heat
                )
                if not is_same_heat:
                    chk_sql = """
                        SELECT s.checked_in, IFNULL(r.score, s.score) as score
                        FROM start_list s
                        LEFT JOIN registrations r 
                            ON s.name = r.name AND s.team_name = r.team_name AND s.group_name = r.group_name 
                            AND (r.event_name = s.event_name OR r.event_name = REPLACE(s.event_name, ' (预赛)', ''))
                        WHERE s.group_name = ? AND s.event_name = ? AND s.gender = ? AND s.heat = ?
                    """
                    active_ath = c.execute(chk_sql, (cur_active['group_name'], cur_active['event_name'], cur_active['gender'], cur_active['heat'])).fetchall()
                    valid_ath = [a for a in active_ath if a['checked_in'] != 2]
                    unscored_count = sum(1 for a in valid_ath if not a['score'] or str(a['score']).strip() == '')
                    
                    if len(valid_ath) > 0 and unscored_count > 0:
                        conn.close()
                        return jsonify({
                            "status": "error", 
                            "msg": f"⚠️ 跑道正忙！终点正在录入【{cur_active['group_name']} {cur_active['event_name']} 第{cur_active['heat']}组】成绩，尚余 {unscored_count} 人未录完！"
                        })
            except Exception:
                pass

        clean_gender = '女' if '女' in gender else ('男' if '男' in gender else gender)
        push_time = datetime.now().strftime('%H:%M:%S')

        # 🌟 关键：查询出本组所有道次的选手名字与单位，供摄像机道次实时渲染
        ath_rows = c.execute("""
            SELECT lane, name, team_name, bib 
            FROM start_list 
            WHERE group_name = ? AND event_name = ? AND gender = ? AND heat = ?
            ORDER BY CAST(lane AS INTEGER) ASC
        """, (g_name, e_name, clean_gender, heat)).fetchall()

        lane_athletes_map = {}
        for r in ath_rows:
            lane_num = str(r['lane']).strip()
            lane_athletes_map[lane_num] = {
                'name': r['name'],
                'team': r['team_name'],
                'bib': r['bib']
            }

        heat_payload = {
            "group_name": g_name,
            "event_name": e_name,
            "gender": clean_gender,
            "heat": heat,
            "push_time": push_time,
            "athletes": lane_athletes_map  # 👈 携带选手道次名单
        }

        c.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('active_track_heat', ?)", 
                  (json.dumps(heat_payload, ensure_ascii=False),))

        c.execute("""
            UPDATE start_list 
            SET is_started = 1 
            WHERE group_name = ? AND event_name = ? AND gender = ? AND heat = ?
        """, (g_name, e_name, clean_gender, heat))

        conn.commit()
        
        # 实时通知所有裁判端（包括摄像端）
        socketio.emit('heat_activated', heat_payload)
        return jsonify({"status": "success", "msg": "发车成功", "data": heat_payload})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
@app.route('/api/get_active_heat', methods=['GET'])
def get_active_heat():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)""")
        row = c.execute("SELECT value FROM system_settings WHERE key='active_track_heat'").fetchone()
        if not row or not row[0]:
            return jsonify({"status": "success", "data": None, "is_finished": True})

        active_data = json.loads(row[0])
        
        chk_sql = """
            SELECT s.checked_in, IFNULL(r.score, s.score) as score
            FROM start_list s
            LEFT JOIN registrations r 
                ON s.name = r.name AND s.team_name = r.team_name AND s.group_name = r.group_name 
                AND (r.event_name = s.event_name OR r.event_name = REPLACE(s.event_name, ' (预赛)', ''))
            WHERE s.group_name = ? AND s.event_name = ? AND s.gender = ? AND s.heat = ?
        """
        ath_list = c.execute(chk_sql, (active_data['group_name'], active_data['event_name'], active_data['gender'], active_data['heat'])).fetchall()
        
        valid_runners = [a for a in ath_list if a['checked_in'] != 2]
        scored_count = sum(1 for a in valid_runners if a['score'] and str(a['score']).strip() != '')
        is_finished = (len(valid_runners) > 0 and scored_count >= len(valid_runners))

        active_data['is_finished'] = is_finished
        active_data['unscored_count'] = len(valid_runners) - scored_count

        return jsonify({"status": "success", "data": active_data, "is_finished": is_finished})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()

@app.route('/api/calculate_detailed_matrix', methods=['POST'])
def calculate_detailed_matrix():
    data = request.json or {}
    g_name = data.get('group_name')
    conn = get_db_connection()
    c = conn.cursor()
    try:
        # 1. 🌟 预先载入当前组别下的所有代表队及附加分，防止0分但有跑操加分的班级漏掉
        team_rows = c.execute("""
            SELECT t.name, t.custom_scores
            FROM cfg_teams t
            LEFT JOIN cfg_groups g ON t.group_id = g.id
            WHERE g.name = ?
            ORDER BY t.sort_order ASC, t.id ASC
        """, (g_name,)).fetchall()

        # 读取系统设定的集体附加项目列（如：跑操比赛、迎面接力等）
        col_row = c.execute("SELECT value FROM sys_config WHERE key = 'custom_score_cols'").fetchone()
        custom_cols = []
        if col_row and col_row['value']:
            try: custom_cols = json.loads(col_row['value'])
            except Exception: custom_cols = []
        if not custom_cols:
            custom_cols = ["跑操比赛"]

        # 初始化各班级的矩阵底表
        matrix = {}
        for tr in team_rows:
            t_name = tr['name']
            s_map = {}
            if tr['custom_scores']:
                try: s_map = json.loads(tr['custom_scores'])
                except Exception: s_map = {}
            
            # 计算该班集体附加分
            c_details = {col: float(s_map.get(col, 0) or 0) for col in custom_cols}
            extra_sum = sum(c_details.values())

            matrix[t_name] = {
                'team': t_name,
                'total': extra_sum,           # 初始总分计入附加分
                'details': {},
                'custom_details': c_details   # 传递给前端渲染集体列
            }

        # 2. 🌟 保持你原版强大的 CTE 接力去重 SQL
        sql = """
        WITH clean_pts AS (
            SELECT 
                team_name, event_name, gender, points
            FROM (
                SELECT 
                    team_name, event_name, gender, points,
                    ROW_NUMBER() OVER (
                        PARTITION BY 
                            CASE 
                                WHEN (event_name LIKE '%4x%' OR event_name LIKE '%4×%' OR event_name LIKE '%4*%' OR event_name LIKE '%接力%' OR event_name LIKE '%集体%' OR event_name LIKE '%团体%') 
                                THEN team_name || '#' || event_name || '#' || gender
                                ELSE team_name || '#' || event_name || '#' || gender || '#' || name
                            END 
                        ORDER BY points DESC
                    ) as rn
                FROM registrations
                WHERE group_name = ? AND points > 0
            )
            WHERE rn = 1
        )
        SELECT team_name, event_name, gender, SUM(points) as pts
        FROM clean_pts
        GROUP BY team_name, event_name, gender
        """
        raw_data = c.execute(sql, (g_name,)).fetchall()
        
        all_core_events = set()
        
        for r in raw_data:
            t = r['team_name']
            full_evt = r['event_name']
            gender = r['gender']
            p = float(r['pts'] or 0)
         
            # 🌟 保持你原有的核心项目名归一化正则提取
            core_evt = re.sub(r"\(.*?\)|（.*?）|决赛|预赛|及格赛|男子|女子|混合|男|女|第一组|第二组|第三组|第四组|第\d+组", "", full_evt).strip()
            if not core_evt:
                core_evt = full_evt
            all_core_events.add(core_evt)
            
            if t not in matrix: 
                matrix[t] = {'team': t, 'total': 0.0, 'details': {}, 'custom_details': {col: 0.0 for col in custom_cols}}
            if core_evt not in matrix[t]['details']: 
                matrix[t]['details'][core_evt] = {'男': 0, '女': 0}
       
            g_key = '男' if '男' in gender else ('女' if '女' in gender else '男')
            matrix[t]['details'][core_evt][g_key] += p
            matrix[t]['total'] += p

        # 四舍五入保留两位小数
        for m in matrix.values():
            m['total'] = round(m['total'], 2)
            
        cols = sorted(list(all_core_events))
        rows = sorted(matrix.values(), key=lambda x: x['total'], reverse=True)

        # 🌟 同时返回常规项目列 columns 与集体附加项目列 custom_columns
        return jsonify({
            'columns': cols, 
            'custom_columns': custom_cols, 
            'rows': rows
        })
    except Exception as e:
        print("详细矩阵计算异常:", e)
        return jsonify({'columns': [], 'custom_columns': [], 'rows': []})
    finally:
        conn.close()
@app.route('/api/get_team_score_details', methods=['POST'])
def get_team_score_details():
    data = request.json or {}
    g_name = str(data.get('group_name') or '').strip()
    t_name = str(data.get('team_name') or '').strip()

    conn = get_db_connection()
    c = conn.cursor()

    try:
        # 1. 查询该班级常规得分（通过 start_list 强力补齐接力真实完赛成绩）
        sql = """
            WITH ranked_scores AS (
                SELECT 
                    r.name, 
                    r.team_name, 
                    r.event_name, 
                    r.gender, 
                    -- 优先取 registrations 的成绩；若为空则自动提取 start_list 中的真实成绩
                    COALESCE(
                        NULLIF(TRIM(r.score), ''), 
                        NULLIF(TRIM(s.score), ''), 
                        ''
                    ) AS score, 
                    r.points, 
                    r.rank,
                    ROW_NUMBER() OVER (
                        PARTITION BY 
                            CASE 
                                WHEN (r.event_name LIKE '%4x%' OR r.event_name LIKE '%4×%' OR r.event_name LIKE '%4*%' OR r.event_name LIKE '%接力%' OR r.event_name LIKE '%集体%' OR r.event_name LIKE '%团体%')
                                THEN r.event_name || '#' || r.gender
                                ELSE r.id
                            END
                        ORDER BY r.points DESC, r.id ASC
                    ) as rn
                FROM registrations r
                LEFT JOIN start_list s 
                    ON TRIM(r.group_name) = TRIM(s.group_name)
                    AND TRIM(r.team_name) = TRIM(s.team_name)
                    AND r.gender = s.gender
                    AND (
                        r.event_name = s.event_name 
                        OR REPLACE(REPLACE(REPLACE(r.event_name, 'x', '×'), 'X', '×'), '*', '×') = REPLACE(REPLACE(REPLACE(s.event_name, 'x', '×'), 'X', '×'), '*', '×')
                        OR s.event_name LIKE '%' || r.event_name || '%'
                        OR r.event_name LIKE '%' || s.event_name || '%'
                    )
                    AND s.score IS NOT NULL 
                    AND TRIM(s.score) != ''
                    AND s.score != '-'
                WHERE TRIM(r.group_name) = ? AND TRIM(r.team_name) = ? AND r.points > 0
            )
            SELECT name, team_name, event_name, gender, score, points, rank
            FROM ranked_scores
            WHERE rn = 1
        """
        rows = c.execute(sql, (g_name, t_name)).fetchall()

        team_details = []
        for r in rows:
            final_rank = r['rank']
            if not final_rank or str(final_rank).strip() == '':
                final_rank = '--'
            else:
                try: final_rank = int(final_rank)
                except Exception: pass

            # 格式化展示成绩：如果接力成绩存在，直接显示；如果仍无成绩才显示 '--'
            score_disp = r['score'] if r['score'] and str(r['score']).strip() != '' else '--'

            team_details.append({
                'event_name': r['event_name'],
                'gender': r['gender'],
                'name': r['name'],
                'score': score_disp,
                'rank': final_rank,
                'points': r['points'],
                'is_custom': False
            })

        # 🌟 2. 查出系统当前生效的集体附加项目
        col_row = c.execute("SELECT value FROM sys_config WHERE key = 'custom_score_cols'").fetchone()
        custom_cols = []
        if col_row and col_row['value']:
            try: custom_cols = json.loads(col_row['value'])
            except Exception: custom_cols = []
        if not custom_cols:
            custom_cols = ["跑操比赛"]

        # 🌟 3. 读取双倍积分规则（默认 9, 7, 6, 5, 4, 3, 2, 1）
        rule_row = c.execute("SELECT score_rule FROM cfg_events WHERE score_rule IS NOT NULL AND score_rule != '' LIMIT 1").fetchone()
        rule_str = rule_row['score_rule'] if rule_row and rule_row['score_rule'] else "9,7,6,5,4,3,2,1"
        rules = [int(x.strip()) for x in rule_str.replace('，', ',').split(',') if x.strip().isdigit()]
        if not rules:
            rules = [9, 7, 6, 5, 4, 3, 2, 1]

        def get_custom_double_rank(val_num):
            if val_num <= 0:
                return '--'
            for idx, r in enumerate(rules):
                if abs(val_num - r * 2) < 0.001:
                    return idx + 1
            for idx, r in enumerate(rules):
                if val_num >= r * 2:
                    return idx + 1
            return '--'

        # 🌟 4. 读取该班级的集体项目并反推计算名次
        team_row = c.execute("""
            SELECT t.custom_scores 
            FROM cfg_teams t
            LEFT JOIN cfg_groups g ON t.group_id = g.id
            WHERE TRIM(t.name) = ? AND (TRIM(g.name) = ? OR ? = '')
            LIMIT 1
        """, (t_name, g_name, g_name)).fetchone()

        if team_row and team_row['custom_scores']:
            try:
                score_dict = json.loads(team_row['custom_scores'])
                for col in custom_cols:
                    try: val = float(score_dict.get(col, 0) or 0)
                    except (ValueError, TypeError): val = 0.0

                    if val > 0:
                        calc_rank = get_custom_double_rank(val)
                        disp_val = int(val) if val == int(val) else val
                        team_details.append({
                            'event_name': col,
                            'gender': '集体',
                            'name': '全班集体',
                            'score': f"{disp_val}分",
                            'rank': calc_rank,
                            'points': disp_val,
                            'is_custom': True
                        })
            except Exception as ex:
                print("解析custom_scores异常:", ex)

        team_details.sort(key=lambda x: (-float(x['points'] or 0), str(x['event_name'])))
        return jsonify(team_details)
    except Exception as e:
        print("获取班级得分明细异常:", e)
        return jsonify([])
    finally:
        conn.close()
@app.route('/api/reset_system', methods=['POST'])
@login_required('admin')
def reset_system():
    data = request.json or {}
    mode = data.get('mode')
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        if mode == 'all':
            # 完全重置：直接清表
            c.execute("DELETE FROM registrations")
            c.execute("DELETE FROM start_list")
            c.execute("DELETE FROM team_auth")
            c.execute("DELETE FROM cfg_groups")
            c.execute("DELETE FROM cfg_teams")
            c.execute("DELETE FROM cfg_events")
            c.execute("DELETE FROM sqlite_sequence")
            c.execute("DELETE FROM sys_config")         
            c.execute("DELETE FROM system_settings")
            c.execute("DELETE FROM cfg_group_records")   
            msg = "系统已完成完全初始化，所有配置与数据已重置。"
        else:
            # 1. 常规清空：清空运动员成绩、名次、积分与试跳明细
            c.execute("""
                UPDATE registrations 
                SET score = '', rank = '', points = 0, record_bonus = 0, relay_leg = '', attempts_json = ''
            """)
            c.execute("""
                UPDATE start_list 
                SET score = '', checked_in = 0, is_started = 0, attempts_json = ''
            """)
            
            # 🌟 2. 核心修复：将所有带有“(决赛)”后缀的跑道，强制退回给“预赛出线/待定”占位符
            c.execute("""
                UPDATE start_list 
                SET name = '第' || lane || '道(决赛待定)', 
                    team_name = '预赛出线', 
                    bib = '',
                    is_manual_final = 0
                WHERE event_name LIKE '%(决赛)%'
            """)
            
            # 🌟 3. 删除由自动晋级或手动发布产生的 registrations 决赛衍生数据，使其彻底消失
            c.execute("""
                DELETE FROM registrations 
                WHERE event_name LIKE '%(决赛)%'
            """)
            
            # 4. 彻底清空发车记录与跑道当前组次占用，杜绝假发车弹窗
            c.execute("DELETE FROM system_settings WHERE key = 'active_track_heat'")
            try:
                c.execute("DELETE FROM active_heat")
            except Exception:
                pass
                
            msg = "✅ 运动员成绩、积分与发车状态已全部重置！旧的决赛名单已彻底退回待定状态。"
            
        conn.commit()
        _cache_store.clear()
        
        # 🌟 广播通知各终端：强制刷新赛程和元数据，消灭旧页面残留
        if 'socketio' in globals() and socketio:
            socketio.emit('track_released', {})
            socketio.emit('score_updated', {})
            socketio.emit('meta_updated')
            socketio.emit('checkin_updated', {})
            
        return jsonify({"status": "success", "msg": msg})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": "操作失败: " + str(e)}), 500
    finally:
        conn.close()
        force_sync_and_upgrade_db()
@app.route('/api/export_teams')
@login_required('admin')
def export_teams():
    conn = get_db_connection()
    try:
        c = conn.cursor()
        query = """
            SELECT 
                g.name as g_name, 
                IFNULL(g.prefix, '-') as g_prefix,
                IFNULL(t.name, '') as t_name, 
                IFNULL(t.leader, '') as t_leader 
            FROM cfg_groups g
            LEFT JOIN cfg_teams t ON CAST(t.group_id AS TEXT) = CAST(g.id AS TEXT)
            ORDER BY g.id ASC, t.id ASC
        """
        rows = c.execute(query).fetchall()
    finally:
        conn.close()
    output = StringIO()
    output.write('\ufeff')
    writer = csv.writer(output)
    writer.writerow(['组别名称', '组别编号前缀', '代表队名称', '领队/教练'])
    
    for r in rows:
        writer.writerow([r['g_name'], r['g_prefix'], r['t_name'], r['t_leader']])
        
    mem = BytesIO()
    mem.write(output.getvalue().encode('utf-8-sig'))
    mem.seek(0)
    return send_file(
        mem, 
        mimetype='text/csv', 
        as_attachment=True, 
        download_name=f'参赛单位与组别名单_{datetime.now().strftime("%Y%m%d")}.csv'
    )

@app.route('/api/import_teams', methods=['POST'])
@login_required('admin')
def import_teams():
    if 'file' not in request.files: 
        return jsonify({"status": "error", "msg": "未上传文件"})
    file = request.files['file']
    
    try:
        stream = StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
        csv_input = csv.reader(stream)
        header = next(csv_input, None)
        
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE")

        groups_map = {row['name']: row['id'] for row in c.execute("SELECT id, name FROM cfg_groups").fetchall()}
        
        success_teams = 0
        success_groups = 0

        for row in csv_input:
            if not row or not row[0].strip():
                continue
            
            g_name = row[0].strip()
            if len(row) >= 4:
                g_prefix = row[1].strip() or '-'
                t_name = row[2].strip()
                t_leader = row[3].strip()
            elif len(row) == 3:
                g_prefix = '-'
                t_name = row[1].strip()
                t_leader = row[2].strip()
            else:
                g_prefix = '-'
                t_name = row[1].strip() if len(row) > 1 else ''
                t_leader = ''

            if g_name not in groups_map:
                new_gid = int(datetime.now().timestamp() * 1000) + random.randint(100, 999)
                c.execute("INSERT INTO cfg_groups (id, name, prefix) VALUES (?, ?, ?)", 
                          (new_gid, g_name, g_prefix))
                groups_map[g_name] = new_gid
                success_groups += 1

            gid = groups_map[g_name]

            if t_name:
                c.execute("INSERT OR REPLACE INTO cfg_teams (group_id, name, leader) VALUES (?, ?, ?)", 
                          (gid, t_name, t_leader))
                success_teams += 1
            
        conn.commit()
        _cache_store.clear()
        return jsonify({"status": "success", "msg": f"✅ 导入成功！共处理 {success_groups} 个新组别，{success_teams} 个代表队！"})
    except Exception as e:
        if 'conn' in locals(): conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        if 'conn' in locals(): conn.close()

@app.route('/api/get_query_nav_events', methods=['GET'])
def get_query_nav_events():
    cache_key = "query_nav_events"
    cached = get_cached_data(cache_key, ttl_seconds=60)
    if cached is not None:
        return jsonify(cached)

    conn = get_db_connection()
    c = conn.cursor()
    try:
        # 1. 仅提取系统比赛名称
        title_row = c.execute("SELECT value FROM sys_config WHERE key='title'").fetchone()
        title = title_row[0] if title_row else "田径运动会"

        # 2. 仅提取组别列表
        groups = [dict(r) for r in c.execute("SELECT id, name FROM cfg_groups ORDER BY id ASC").fetchall()]

        # 3. 仅提取去重后的赛程项目名（彻底摆脱几十KB的道次明细全表）
        rows = c.execute("SELECT DISTINCT event_name FROM start_list WHERE event_name != ''").fetchall()
        
        all_events = []
        final_events = []
        seen = set()

        for r in rows:
            raw_name = r['event_name']
            clean_name = re.sub(r'[\(（]场地\d+[\)）]', '', raw_name).strip()
            if clean_name and clean_name not in seen:
                seen.add(clean_name)
                all_events.append(clean_name)
                if '决赛' in clean_name and '预赛' not in clean_name:
                    final_events.append(clean_name)

        # 排序规则
        all_events.sort(key=lambda x: (
            0 if '预赛' in x else 1,
            re.sub(r'[\(（].*?[\)）]', '', x)
        ))
        final_events.sort()

        result = {
            "status": "success",
            "title": title,
            "groups": groups,
            "all_events": all_events,
            "final_events": final_events
        }
        set_cached_data(cache_key, result, ttl_seconds=60)
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
@app.route('/api/get_statistics')
def get_statistics():
    conn = get_db_connection() 
    c = conn.cursor()
    try:
        group_stats = c.execute("""
            SELECT group_name, gender, COUNT(DISTINCT name) as count 
            FROM registrations 
            WHERE group_name IS NOT NULL AND name != ''
              AND event_name NOT LIKE '%决赛%'
            GROUP BY group_name, gender
        """).fetchall()

        all_regs = c.execute("""
            SELECT event_name, group_name, gender 
            FROM registrations 
            WHERE event_name != '' AND group_name != ''
              AND event_name NOT LIKE '%决赛%'
        """).fetchall()

        events_map = {}
        event_group_map = {}

        for r in all_regs:
            raw_e = r['event_name']
            g_name = r['group_name']
            gen = r['gender']

            core_e = re.sub(r'[\(（].*?[\)）]', '', raw_e).strip()
            if not core_e:
                core_e = raw_e

            events_map[core_e] = events_map.get(core_e, 0) + 1
            eg_key = (core_e, g_name, gen)
            event_group_map[eg_key] = event_group_map.get(eg_key, 0) + 1

        event_stats = [{"event_name": k, "count": v} for k, v in events_map.items()]
        event_stats.sort(key=lambda x: x['count'], reverse=True)

        event_group_stats = [
            {"event_name": k[0], "group_name": k[1], "gender": k[2], "count": v}
            for k, v in event_group_map.items()
        ]

        team_engagement = c.execute("""
            SELECT team_name, COUNT(DISTINCT name) as athlete_count 
            FROM registrations 
            WHERE event_name NOT LIKE '%决赛%'
            GROUP BY team_name 
            ORDER BY athlete_count DESC 
            LIMIT 5
        """).fetchall()

        total_athletes = c.execute("""
            SELECT COUNT(DISTINCT team_name || name) 
            FROM registrations 
            WHERE name != '' AND event_name NOT LIKE '%决赛%'
        """).fetchone()[0] or 0

        total_participations = sum(events_map.values())
        
        return jsonify({
            "group_gender": [dict(r) for r in group_stats],
            "events": event_stats,
            "event_group_details": event_group_stats,
            "top_teams": [dict(r) for r in team_engagement], 
            "total_athletes": total_athletes,
            "total_participations": total_participations
        })
    except Exception:
        return jsonify({
            "group_gender": [], "events": [], "event_group_details": [],
            "top_teams": [], "total_athletes": 0, "total_participations": 0
        })
    finally:
        conn.close()

@app.route('/api/get_data')
def get_data_admin():
    current_role = session.get('user_role')
    if current_role not in ['admin']:
        cache_key = "get_data_public"
        cached = get_cached_data(cache_key, ttl_seconds=10)
        if cached is not None:
            return jsonify(cached)

    conn = get_db_connection()
    c = conn.cursor()    
    try:
        # 🌟 按照手动保存的权重排序，没有权重时降级为 rowid
        try:
            db_groups = [dict(r) for r in c.execute("SELECT id, name, prefix FROM cfg_groups ORDER BY IFNULL(sort_order, rowid) ASC").fetchall()]
        except Exception:
            db_groups = [dict(r) for r in c.execute("SELECT id, name, prefix FROM cfg_groups ORDER BY rowid ASC").fetchall()]
        
        try:
            db_teams = [dict(r) for r in c.execute("SELECT id, group_id, name, leader, coach, phone FROM cfg_teams ORDER BY IFNULL(sort_order, rowid) ASC").fetchall()]
        except Exception:
            db_teams = [dict(r) for r in c.execute("SELECT id, group_id, name, leader, coach, phone FROM cfg_teams ORDER BY rowid ASC").fetchall()]
        for t in db_teams: t['groupId'] = t['group_id']
        db_events = [dict(r) for r in c.execute("SELECT * FROM cfg_events").fetchall()]
        
        db_schedule = []
        try:
            raw_sch = c.execute('''SELECT id, group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field, checked_in, IFNULL(is_started, 0) as is_started, IFNULL(score, '') as score 
                     FROM start_list ORDER BY time_index ASC, CAST(heat AS INTEGER) ASC, CAST(lane AS INTEGER) ASC''').fetchall()
            for r in raw_sch:
                item = dict(r)
                item['groupName'] = r['group_name']
                item['eventName'] = r['event_name']
                item['teamName'] = r['team_name']
                item['isField'] = (r['is_field'] == 1)
                item['estTime'] = r['est_time']
                item['timeIndex'] = r['time_index']
                item['totalLanes'] = r['total_lanes']
                item['checkedIn'] = r['checked_in']
                item['is_started'] = r['is_started']
                item['score'] = r['score']
                db_schedule.append(item)
        except Exception as err: 
            print(f"读取编排表异常: {err}")
        
        # 基础公开数据包
        response_data = {
            "status": "success",
            "groups": db_groups, 
            "teams": db_teams, 
            "events": db_events, 
            "schedule": db_schedule
        }

        # 🌟 安全加固：只有已登录的管理员，才返回全量选手名单与系统核心敏感配置！
        if current_role == 'admin':
            raw_regs = c.execute("SELECT * FROM registrations").fetchall()
            athletes_map = {}
            for r in raw_regs:
                key = f"{r['team_id']}_{r['name']}"
                if key not in athletes_map:
                    athletes_map[key] = { "id": r['id'], "teamId": int(r['team_id']) if r['team_id'] else 0, "name": r['name'], "gender": r['gender'], "bib": r['bib'] or "", "events": [], "relay_legs": {} }
                athletes_map[key]["events"].append(r['event_name'])
                try:
                    if 'relay_leg' in r.keys() and r['relay_leg']:
                        athletes_map[key]["relay_legs"][r['event_name']] = str(r['relay_leg'])
                except Exception: pass
            
            config = {r['key']: r['value'] for r in c.execute("SELECT * FROM sys_config").fetchall()}
            response_data["athletes"] = list(athletes_map.values())
            response_data["config"] = config
        else:
            # 普通裁判/查询端仅开放公共标题，绝不透传其他系统设置
            title_row = c.execute("SELECT value FROM sys_config WHERE key='title'").fetchone()
            response_data["config"] = {"title": title_row[0] if title_row else "田径运动会"}
            response_data["athletes"] = []

        if current_role != 'admin':
            set_cached_data("get_data_public", response_data, ttl_seconds=20)
            
        return jsonify(response_data)
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
@app.route('/api/save_relay_legs', methods=['POST'])
def save_relay_legs():
    current_role = session.get('user_role')
    if current_role not in ['team', 'admin']:
        return jsonify({"status": "error", "msg": "登录会话已超时，请重新登录！"}), 401
        
    data = request.json or {}
    team_id = data.get('team_id')
    event_name = data.get('event_name', '').strip()
    gender = data.get('gender', '').strip()
    legs = data.get('legs', {})
    
    # 1. 严格锁定班级权限（禁止领队代改他班）
    if current_role == 'team':
        session_team_id = session.get('team_id')
        if not session_team_id or str(team_id) != str(session_team_id):
            return jsonify({"status": "error", "msg": "越权操作：仅允许提交本班代表队的接力棒次！"}), 403        
    
    if not team_id or not event_name:
        return jsonify({"status": "error", "msg": "参数缺失：班级或项目不能为空！"}), 400

    # 规范项目名，彻底去除预决赛后缀与乘号差异
    clean_core = re.sub(r'[\(（].*?[\)）]', '', event_name).replace('*', '×').replace('x', '×').replace('X', '×').strip()
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        
        # 2. 查出队伍与归属组别，若队伍不存在直接拦截
        t_row = c.execute("""
            SELECT t.name as team_name, g.name as group_name 
            FROM cfg_teams t
            JOIN cfg_groups g ON t.group_id = g.id
            WHERE t.id = ?
        """, (str(team_id),)).fetchone()
        
        if not t_row:
            conn.close()
            return jsonify({"status": "error", "msg": "代表队或所属组别不存在！"}), 404

        team_name = t_row['team_name']
        group_name = t_row['group_name']

        # 3. 校验提交的所有队员 ID 是否 100% 属于该班级、该组别以及该接力项目
        valid_reg_ids = []
        for leg_num, reg_id in legs.items():
            if not reg_id:
                continue
            try:
                r_id_int = int(reg_id)
            except (ValueError, TypeError):
                continue

            # 校验该学生报名记录是否属于当前 team_id
            check_row = c.execute("""
                SELECT id FROM registrations 
                WHERE id = ? AND team_id = ?
                  AND (gender = ? OR ? = '' OR gender = '混合')
                  AND (REPLACE(REPLACE(event_name, 'x', '×'), '*', '×') LIKE ? OR event_name = ?)
            """, (r_id_int, str(team_id), gender, gender, f"%{clean_core}%", event_name)).fetchone()

            if not check_row:
                conn.close()
                return jsonify({
                    "status": "error", 
                    "msg": f"安全拦截：第 {leg_num} 棒所选选手不存在、非本班成员或未报名此项目！"
                }), 400
            
            valid_reg_ids.append((str(leg_num), r_id_int))

        # 4. 重置本班在 registrations 表中的该项目棒次
        c.execute("""
            UPDATE registrations 
            SET relay_leg = '' 
            WHERE team_id = ? 
              AND (gender = ? OR ? = '' OR gender = '混合')
              AND (REPLACE(REPLACE(event_name, 'x', '×'), '*', '×') LIKE ? OR event_name = ?)
        """, (str(team_id), gender, gender, f"%{clean_core}%", event_name))
        
        # 5. 安全写入校验通过的棒次
        for leg_str, r_id_int in valid_reg_ids:
            c.execute("""
                UPDATE registrations 
                SET relay_leg = ? 
                WHERE id = ? AND team_id = ?
            """, (leg_str, r_id_int, str(team_id)))
        
        # 6. 读取排好的棒次，更新 start_list 道次名单
        runners = c.execute("""
            SELECT name, relay_leg FROM registrations 
            WHERE team_id = ? AND (gender = ? OR ? = '' OR gender = '混合')
              AND (REPLACE(REPLACE(event_name, 'x', '×'), '*', '×') LIKE ? OR event_name = ?)
              AND relay_leg != '' AND relay_leg IS NOT NULL
            ORDER BY CAST(relay_leg AS INTEGER) ASC
        """, (str(team_id), gender, gender, f"%{clean_core}%", event_name)).fetchall()

        if runners:
            display_relay_name = "  ".join([f"{r['relay_leg']}棒:{r['name']}" for r in runners])
            # 严格双重约束：同时锁定 team_name + 强制非空 group_name，杜绝同名班级交叉覆盖
            c.execute("""
                UPDATE start_list 
                SET name = ? 
                WHERE team_name = ? 
                  AND group_name = ?
                  AND (gender = ? OR ? = '' OR gender = '混合')
                  AND (REPLACE(REPLACE(event_name, 'x', '×'), '*', '×') LIKE ? OR event_name = ?)
            """, (display_relay_name, team_name, group_name, gender, gender, f"%{clean_core}%", event_name))

        conn.commit()

        # 失效受影响的缓存
        invalidate_event_cache(event_name, group_name)

        # 广播通知裁判端和查询端刷新检录名单
        if 'socketio' in globals() and socketio:
            socketio.emit('score_updated', {'event_name': clean_core, 'group_name': group_name})
            socketio.emit('checkin_updated', {})

        return jsonify({"status": "success", "msg": "接力棒次保存成功，裁判端检录名单已实时同步！"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": f"保存失败: {str(e)}"}), 500
    finally:
        conn.close()
# ==================== app.py 约第 710 行新增 ====================
@app.route('/api/sync_relay_legs_to_schedule', methods=['POST'])
@login_required('admin')
def sync_relay_legs_to_schedule():
    """
    一键同步接力棒次专用安全接口：
    仅将各班提交的 1~4 棒姓名更新到 start_list 接力道次的 name 字段中。
    【绝对安全】：绝不删除任何表、绝不重置任何成绩(score)、绝不改变任何检录状态(checked_in)！
    """
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        
        # 1. 提取 registrations 中各班级领队已提交的接力棒次
        runners = c.execute("""
            SELECT team_name, group_name, gender, event_name, name, relay_leg
            FROM registrations
            WHERE relay_leg != '' AND relay_leg IS NOT NULL
              AND (event_name LIKE '%接力%' OR event_name LIKE '%4x%' OR event_name LIKE '%4×%' OR event_name LIKE '%4*%')
            ORDER BY team_name, group_name, gender, event_name, CAST(relay_leg AS INTEGER) ASC
        """).fetchall()
        
        # 2. 按班级和项目聚合为 "1棒:张三  2棒:李四..." 格式
        relay_map = {}
        for r in runners:
            clean_core = re.sub(r'[\(（].*?[\)）]', '', r['event_name']).replace('*', '×').replace('x', '×').replace('X', '×').strip()
            key = (r['team_name'], r['group_name'], r['gender'], clean_core)
            if key not in relay_map:
                relay_map[key] = []
            relay_map[key].append(f"{r['relay_leg']}棒:{r['name']}")
        
        # 3. 仅对 start_list 执行纯字段 UPDATE，只改 name，不动任何成绩和检录状态！
        updated_teams = 0
        for (team_name, group_name, gender, clean_core), leg_list in relay_map.items():
            display_name = "  ".join(leg_list)
            c.execute("""
                UPDATE start_list 
                SET name = ? 
                WHERE team_name = ? 
                  AND group_name = ?
                  AND (gender = ? OR ? = '' OR gender = '混合')
                  AND (REPLACE(REPLACE(REPLACE(event_name, 'x', '×'), 'X', '×'), '*', '×') LIKE ? OR event_name LIKE ?)
            """, (display_name, team_name, group_name, gender, gender, f"%{clean_core}%", f"%{clean_core}%"))
            updated_teams += 1
            
        conn.commit()
        
        # 4. 失效接力相关道次缓存，并通过 WebSocket 即时通知检录裁判与发令裁判刷新
        if '_cache_store' in globals():
            for k in list(_cache_store.keys()):
                if 'startlist_' in k and ('接力' in k or '4x' in k or '4×' in k):
                    _cache_store.pop(k, None)
                    
        if 'socketio' in globals() and socketio:
            socketio.emit('checkin_updated', {})
            socketio.emit('score_updated', {})
            
        return jsonify({
            "status": "success", 
            "msg": f"🎉 接力棒次已安全同步！共更新 {updated_teams} 个接力代表队棒次。\n检录裁判端与起点发令端已实时更新，所有既有成绩与检录记录完好无损！"
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": f"同步失败: {str(e)}"}), 500
    finally:
        conn.close()
@app.route('/api/save_sandtable', methods=['POST'])
@login_required('admin')
def save_sandtable():
    data = request.json or {}
    map_data = data.get('map', {})
    sandtable_json = json.dumps(map_data, ensure_ascii=False)
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("CREATE TABLE IF NOT EXISTS sys_config (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT OR REPLACE INTO sys_config (key, value) VALUES ('manual_schedule_map', ?)", (sandtable_json,))
        conn.commit()
        return jsonify({"status": "success", "msg": "沙盘日程已安全同步至服务器数据库！"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
@app.route('/api/save_config', methods=['POST'])
@login_required('admin')
def save_config():
    data = request.json or {}
    conn = get_db_connection()
    c = conn.cursor()
    try:
        try: c.execute("ALTER TABLE cfg_groups ADD COLUMN sort_order INTEGER DEFAULT 0")
        except Exception: pass
        try: c.execute("ALTER TABLE cfg_teams ADD COLUMN sort_order INTEGER DEFAULT 0")
        except Exception: pass

        if 'groups' in data:
            c.execute("DELETE FROM cfg_groups")
            for idx, g in enumerate(data['groups']):
                c.execute("INSERT OR REPLACE INTO cfg_groups (id, name, prefix, sort_order) VALUES (?, ?, ?, ?)", 
                          (str(g['id']), g['name'], g.get('prefix', '-'), idx))
                  
        if 'teams' in data:
            old_custom = {r['id']: r['custom_scores'] for r in c.execute("SELECT id, custom_scores FROM cfg_teams").fetchall()}
            c.execute("DELETE FROM cfg_teams")
            for idx, t in enumerate(data['teams']):
                  c.execute("""
                         INSERT OR REPLACE INTO cfg_teams (id, group_id, name, leader, coach, phone, sort_order, custom_scores) 
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                  """, (
                         str(t['id']), str(t.get('groupId') or t.get('group_id')), 
                         t['name'], t.get('leader', '') or '', t.get('coach', '') or '', t.get('phone', '') or '', idx,
                         old_custom.get(str(t['id']), '{}')
                  ))
                  
        if 'events' in data:
            c.execute("DELETE FROM cfg_events")
            for e in data['events']: 
                rule = e.get('scoreRule') or e.get('score_rule') or '9,7,6,5,4,3,2,1'
                rec = e.get('record') or ''
                bonus = e.get('recordBonus') or e.get('record_bonus') or 0
                
                limit_val = e.get('limit') if e.get('limit') is not None else (e.get('limit_count') if e.get('limit_count') is not None else 8)
                dur_val = e.get('duration') if e.get('duration') is not None else 5
                ven_val = e.get('venueCount') if e.get('venueCount') is not None else (e.get('venue_count') if e.get('venue_count') is not None else 1)
                
                sql = '''INSERT OR REPLACE INTO cfg_events 
                    (id, name, type, gender, score_rule, record, record_bonus, 
                     is_double_score, need_lane, has_prelim, is_relay, is_team, limit_count, allowed_groups, duration, venue_count, is_fun) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''
                params = (
                    str(e.get('id', '')), e.get('name', ''), e.get('type', ''), e.get('gender', '双性'), 
                    str(rule), str(rec), str(bonus), 
                    to_bool_str(e.get('isDoubleScore') or e.get('is_double_score')), 
                    to_bool_str(e.get('needLane') or e.get('need_lane')), 
                    to_bool_str(e.get('hasPrelim') or e.get('has_prelim')), 
                    to_bool_str(e.get('isRelay') or e.get('is_relay')), 
                    to_bool_str(e.get('isTeam') or e.get('is_team')),
                    int(limit_val),
                    str(e.get('allowedGroups') or e.get('allowed_groups') or ''),
                    float(dur_val), int(ven_val),
                    to_bool_str(e.get('isFun') or e.get('is_fun'))
                )
                c.execute(sql, params)
                
        if 'config' in data:
            for k, v in data['config'].items(): 
                c.execute("REPLACE INTO sys_config (key, value) VALUES (?, ?)", (k, str(v)))
                
        conn.commit()
        _cache_store.clear()
        return jsonify({"status": "success", "msg": "✅ 配置及参赛单位已全量同步写入数据库！"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": "保存失败: " + str(e)})
    finally:
        conn.close()

@app.route('/api/save_team_staff', methods=['POST'])
def save_team_staff():
    current_role = session.get('user_role')
    if current_role not in ['team', 'admin']:
        return jsonify({"status": "error", "msg": "未登录或登录已超时"}), 401
    
    data = request.json or {}
    # 严格防止领队越权
    if current_role == 'team':
        team_id = str(session.get('team_id'))
    else:
        team_id = str(data.get('team_id') or session.get('team_id'))

    leader = str(data.get('leader', '')).strip()
    coach = str(data.get('coach', '')).strip()
    phone = str(data.get('phone', '')).strip()

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("""
            UPDATE cfg_teams 
            SET leader = ?, coach = ?, phone = ?
            WHERE id = ?
        """, (leader, coach, phone, team_id))
        conn.commit()
        return jsonify({"status": "success", "msg": "领队、教练及联系电话保存成功！"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()

def team_scope_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        role = session.get('user_role')
        if not role:
            return jsonify({"status": "error", "msg": "未登录或会话超时"}), 401
        if role == 'admin':
            return f(*args, **kwargs)
        if role == 'team':
            req_team_id = kwargs.get('team_id') or (request.json or {}).get('team_id')
            if req_team_id and str(req_team_id) != str(session.get('team_id')):
                return jsonify({"status": "error", "msg": "越权访问：仅允许操作本代表队数据"}), 403
            return f(*args, **kwargs)
        return jsonify({"status": "error", "msg": "无权限访问"}), 403
    return decorated_function

# ========================================================
# 2. 修复 /api/team_members 越权泄漏
# ========================================================
@app.route('/api/team_members/<int:team_id>')
@team_scope_required
def get_team_members(team_id):
    conn = get_db_connection()
    try:
        c = conn.cursor()
        rows = c.execute("""
            SELECT id, name, gender, event_name, relay_leg 
            FROM registrations 
            WHERE team_id = ?
            ORDER BY id ASC
        """, (str(team_id),)).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()

# ========================================================
# 3. 修复 /api/batch_update_bibs 权限漏洞
# ========================================================
@app.route('/api/batch_update_bibs', methods=['POST'])
@login_required('admin')
def batch_update_bibs():
    data = request.json or {}
    athletes = data.get('athletes', [])
    if not athletes:
        return jsonify({"status": "error", "msg": "更新列表为空"}), 400

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        for a in athletes:
            c.execute("""
                UPDATE registrations 
                SET bib = ? 
                WHERE team_id = ? AND name = ?
            """, (str(a.get('bib', '')).strip(), str(a.get('teamId', '')), a.get('name', '')))
        conn.commit()
        if '_cache_store' in globals():
            _cache_store.clear()
        return jsonify({"status": "success", "msg": "号码已成功固化到数据库！"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)}), 500
    finally:
        conn.close()
@app.route('/api/rename_athlete', methods=['POST'])
@login_required('admin')
def rename_athlete():
    data = request.json or {}
    old_name = str(data.get('old_name', '')).strip()
    new_name = str(data.get('new_name', '')).strip()
    team_id = str(data.get('team_id', '')).strip()

    if not old_name or not new_name:
        return jsonify({"status": "error", "msg": "原姓名和新姓名均不能为空"}), 400
    if old_name == new_name:
        return jsonify({"status": "error", "msg": "新姓名与原姓名一致，无需修改"}), 400

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")

        # 1. 查询队伍名
        t_row = c.execute("SELECT name FROM cfg_teams WHERE id = ?", (team_id,)).fetchone()
        team_name = t_row['name'] if t_row else ''

        # 2. 检查本班是否已有同名的新选手，避免冲突
        conflict = c.execute("SELECT 1 FROM registrations WHERE team_id = ? AND name = ?", (team_id, new_name)).fetchone()
        if conflict:
            conn.close()
            return jsonify({"status": "error", "msg": f"该代表队已存在名为【{new_name}】的运动员，请勿重名"}), 400

        # 3. 更新报名表 (registrations)
        c.execute("UPDATE registrations SET name = ? WHERE team_id = ? AND name = ?", (new_name, team_id, old_name))

        # 4. 联动更新道次赛程表 (start_list)
        if team_name:
            c.execute("UPDATE start_list SET name = ? WHERE team_name = ? AND name = ?", (new_name, team_name, old_name))
            # 兼容接力棒次中嵌入的名字 (如 "1棒:张三")
            c.execute("""
                UPDATE start_list 
                SET name = REPLACE(name, ?, ?) 
                WHERE team_name = ? AND name LIKE '%' || ? || '%'
            """, (old_name, new_name, team_name, old_name))

        conn.commit()

        # 5. 清理全局缓存
        if '_cache_store' in globals():
            _cache_store.clear()

        # 6. 广播通知裁判端、检录端与查询端实时刷新名单
        if 'socketio' in globals() and socketio:
            socketio.emit('checkin_updated', {})
            socketio.emit('score_updated', {})
            socketio.emit('meta_updated')

        return jsonify({"status": "success", "msg": f"已将【{old_name}】成功更名为【{new_name}】！道次表与裁判端已实时同步。"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": f"修改失败: {str(e)}"}), 500
    finally:
        conn.close()
@app.route('/api/add_athlete', methods=['POST'])
def add_athlete():
    if 'user_role' not in session:
        return jsonify({"status": "error", "msg": "未登录或登录已过期，请重新登录！"}), 401
    
    data = request.json or {}
    user_role = session.get('user_role')
    
    team_id = str(data.get('team_id', '')).strip()
    group_id = str(data.get('group_id', '')).strip()
    name = str(data.get('name', '')).strip()
    gender = str(data.get('gender', '')).strip()
    bib = str(data.get('bib', '')).strip()
    selected_events = data.get('events', [])
    
    if not name:
        return jsonify({"status": "error", "msg": "姓名不能为空！"})
    if not selected_events:
        return jsonify({"status": "error", "msg": "请至少选择一个项目！"})

    if user_role == 'team':
        if team_id != str(session.get('team_id')):
            return jsonify({"status": "error", "msg": "越权操作：领队只能为本班学生报名！"}), 403
            
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE") 

        if user_role == 'team':
            deadline_row = c.execute("SELECT value FROM sys_config WHERE key='regDeadline'").fetchone()
            if deadline_row and deadline_row[0]:
                try:
                    deadline_dt = datetime.strptime(deadline_row[0], "%Y-%m-%dT%H:%M")
                    if datetime.now() > deadline_dt:
                        conn.close()
                        return jsonify({"status": "error", "msg": f"报名通道已关闭！截止时间为：{deadline_row[0].replace('T', ' ')}"})
                except Exception:
                    pass
        
        def get_cfg_val(key, default):
            row = c.execute("SELECT value FROM sys_config WHERE key=?", (key,)).fetchone()
            return int(row[0]) if row else default
        
        MAX_PER_EVENT = get_cfg_val('maxPerEvent', 3)
        MAX_TOTAL = get_cfg_val('maxTotal', 20)
        MAX_MALE = get_cfg_val('maxMale', 10)
        MAX_FEMALE = get_cfg_val('maxFemale', 10)
        MAX_PER_PERSON = get_cfg_val('maxPerPerson', 2)

        comp_count = 0
        for evt in selected_events:
            evt_info = c.execute("SELECT type, is_fun FROM cfg_events WHERE name=?", (evt,)).fetchone()
            is_fun = evt_info and (evt_info['type'] in ['趣味', '趣味项目', '健康测试'] or '趣味' in str(evt_info['type']) or '健康' in str(evt_info['type']) or str(evt_info['is_fun']) == '1')
            if not is_fun:
                comp_count += 1
        if comp_count > MAX_PER_PERSON:
            conn.close()
            return jsonify({"status": "error", "msg": f"每人最多限报 {MAX_PER_PERSON} 项竞技项目！"})

        if gender == '男':
            cur_males = c.execute("SELECT COUNT(DISTINCT name) FROM registrations WHERE team_id=? AND gender='男' AND name!=?", (team_id, name)).fetchone()[0]
            if cur_males >= MAX_MALE:
                conn.close()
                return jsonify({"status": "error", "msg": f"本班男生报名人数已达上限（{MAX_MALE}人）！"})
        elif gender == '女':
            cur_females = c.execute("SELECT COUNT(DISTINCT name) FROM registrations WHERE team_id=? AND gender='女' AND name!=?", (team_id, name)).fetchone()[0]
            if cur_females >= MAX_FEMALE:
                conn.close()
                return jsonify({"status": "error", "msg": f"本班女生报名人数已达上限（{MAX_FEMALE}人）！"})

        current_team_count = c.execute("SELECT COUNT(DISTINCT name) FROM registrations WHERE team_id=? AND name!=?", (team_id, name)).fetchone()[0]
        if current_team_count >= MAX_TOTAL:
            conn.close()
            return jsonify({"status": "error", "msg": f"报名失败！本班总人数已达上限（{MAX_TOTAL}人）！"})
            
        c.execute("DELETE FROM registrations WHERE team_id=? AND name=?", (team_id, name))
        
        for evt in selected_events:
            evt_info = c.execute("SELECT type, is_relay, gender, limit_count FROM cfg_events WHERE name=?", (evt,)).fetchone()
            if not evt_info:
                evt_info = c.execute("SELECT type, is_relay, gender, limit_count FROM cfg_events WHERE name LIKE ?", (f"%{evt}%",)).fetchone()
                
            if evt_info:
                is_fun = (evt_info['type'] == '趣味' or '趣味' in str(evt_info['type']))
                if is_fun:
                    continue
                
                is_relay = (str(evt_info['is_relay']) == '1' or str(evt_info['is_relay']).lower() == 'true')
                if evt_info['gender'] == '混合' and is_relay:
                    current_limit = 10
                elif is_relay:
                    current_limit = 4
                else:
                    current_limit = MAX_PER_EVENT
                
                count_in_evt = c.execute("SELECT COUNT(*) FROM registrations WHERE team_id=? AND event_name=? AND gender=?", (team_id, evt, gender)).fetchone()[0]
                if count_in_evt >= current_limit:
                    conn.close()
                    return jsonify({"status": "error", "msg": f"项目【{evt}】本班【{gender}生】名额（限报 {current_limit} 人）已满，无法报名！"})

        g_info = c.execute("SELECT name FROM cfg_groups WHERE id=?", (group_id,)).fetchone()
        t_info = c.execute("SELECT name FROM cfg_teams WHERE id=?", (team_id,)).fetchone()
        g_name = g_info[0] if g_info else "未知组别"
        t_name = t_info[0] if t_info else "未知班级"
        submit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for evt in selected_events:
            c.execute("""INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, submit_time) 
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", 
                      (group_id, g_name, team_id, t_name, name, gender, bib, evt, submit_time))
        
        conn.commit()
        return jsonify({"status": "success", "msg": "🎉 报名信息已成功录入系统！"})
        
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": f"数据库写入异常: {str(e)}"})
    finally:
        conn.close()

@app.route('/api/batch_submit_team_athletes', methods=['POST'])
def batch_submit_team_athletes():
    if session.get('user_role') != 'team':
        return jsonify({"status": "error", "msg": "未登录或登录已过期，请重新登录！"}), 401
        
    data = request.json or {}
    team_id = str(session.get('team_id'))
    group_id = str(session.get('group_id'))
    athletes_list = data.get('athletes', [])
    
    if not athletes_list:
        return jsonify({"status": "error", "msg": "提交名单为空，请先添加学生！"})

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        
       # 1. 报名截止时间校验
        deadline_row = c.execute("SELECT value FROM sys_config WHERE key='regDeadline'").fetchone()
        if deadline_row and deadline_row[0]:
            try:
                raw_dl = str(deadline_row[0]).strip().replace('T', ' ')
                if len(raw_dl) == 16:
                    raw_dl += ':00'
                cur_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if cur_time > raw_dl:
                    conn.close()
                    return jsonify({"status": "error", "msg": f"报名通道已关闭！截止时间为：{raw_dl}"}), 403
            except Exception:
                pass
	

        # 2. 读取系统各项参数配置（带默认保底）
        def get_cfg_val(key, default):
            row = c.execute("SELECT value FROM sys_config WHERE key=?", (key,)).fetchone()
            return int(row[0]) if row and str(row[0]).isdigit() else default
            
        MAX_TOTAL = get_cfg_val('maxTotal', 20)
        MAX_MALE = get_cfg_val('maxMale', 10)
        MAX_FEMALE = get_cfg_val('maxFemale', 10)
        MAX_PER_PERSON = get_cfg_val('maxPerPerson', 2)
        MAX_PER_EVENT = get_cfg_val('maxPerEvent', 3)

        # 3. 筛选出报名了“竞技项目”的选手（纯趣味项目选手不占全队总人数和男女限额）
        comp_athletes = []
        for ath in athletes_list:
            has_comp = False
            for evt in ath.get('events', []):
                evt_info = c.execute("SELECT type, is_fun FROM cfg_events WHERE name=?", (evt,)).fetchone()
                is_fun = evt_info and (evt_info['type'] in ['趣味', '趣味项目', '健康测试'] or '趣味' in str(evt_info['type']) or '健康' in str(evt_info['type']) or str(evt_info['is_fun']) == '1')
                if not is_fun:
                    has_comp = True
                    break
            if has_comp:
                comp_athletes.append(ath)

        # 4. 校验代表队竞技总人数与男女生人数上限
        if len(comp_athletes) > MAX_TOTAL:
            return jsonify({"status": "error", "msg": f"全队竞技总人数（{len(comp_athletes)}人）超过系统上限（{MAX_TOTAL}人）！纯趣味项目选手不计入此限额。"})

        male_comp_count = sum(1 for a in comp_athletes if a.get('gender') == '男')
        female_comp_count = sum(1 for a in comp_athletes if a.get('gender') == '女')
        
        if male_comp_count > MAX_MALE:
            return jsonify({"status": "error", "msg": f"全队男生竞技人数（{male_comp_count}人）超过系统上限（{MAX_MALE}人）！"})
        if female_comp_count > MAX_FEMALE:
            return jsonify({"status": "error", "msg": f"全队女生竞技人数（{female_comp_count}人）超过系统上限（{MAX_FEMALE}人）！"})

        # 5. 校验个人限报项数与单项每队限报名额（趣味项目完全豁免）
        event_counts = {}
        for ath in athletes_list:
            gender = ath.get('gender')
            comp_events = []
            for evt in ath.get('events', []):
                evt_info = c.execute("SELECT type, is_relay, is_fun FROM cfg_events WHERE name=?", (evt,)).fetchone()
                is_fun = evt_info and (evt_info['type'] in ['趣味', '趣味项目', '健康测试'] or '趣味' in str(evt_info['type']) or '健康' in str(evt_info['type']) or str(evt_info['is_fun']) == '1')
                is_relay = evt_info and (str(evt_info['is_relay']) == '1' or str(evt_info['is_relay']).lower() == 'true')

                # 趣味项目既不占用个人项数指标，也不受每队单项人数限制
                if not is_fun:
                    if not is_relay:
                        comp_events.append(evt)
                    
                    key = f"{evt}_{gender}"
                    event_counts[key] = event_counts.get(key, 0) + 1
                    limit = 4 if is_relay else MAX_PER_EVENT
                    if event_counts[key] > limit:
                        return jsonify({"status": "error", "msg": f"项目【{evt}】本班【{gender}生】已报 {event_counts[key]} 人，超出限额 {limit} 人！"})

            if len(comp_events) > MAX_PER_PERSON:
                return jsonify({"status": "error", "msg": f"学生【{ath.get('name')}】报名个人竞技项（{len(comp_events)}项）超过限额（{MAX_PER_PERSON}项）！"})

        # 6. 获取组别与班级名称元数据
        g_info = c.execute("SELECT name FROM cfg_groups WHERE id=?", (group_id,)).fetchone()
        t_info = c.execute("SELECT name FROM cfg_teams WHERE id=?", (team_id,)).fetchone()
        g_name = g_info[0] if g_info else "未知组别"
        t_name = t_info[0] if t_info else "未知班级"
        submit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 7. 清空本队现有报名，执行全量批量写入
        c.execute("DELETE FROM registrations WHERE team_id=?", (team_id,))
        
        insert_rows = []
        for ath in athletes_list:
            name = ath.get('name', '').strip()
            gender = ath.get('gender')
            bib = ath.get('bib', '').strip()
            for evt in ath.get('events', []):
                insert_rows.append((
                    group_id, g_name, team_id, t_name, name, gender, bib, evt, submit_time
                ))

        c.executemany("""
            INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, submit_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, insert_rows)
        
        conn.commit()
        
        # 清除只读缓存，保证管理端与查询端立刻看到最新数据
        _cache_store.clear()

        return jsonify({
            "status": "success", 
            "msg": f"🎉 全班共 {len(athletes_list)} 名运动员名单已成功批量提交并锁定！"
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": f"保存失败: {str(e)}"}), 500
    finally:
        conn.close()

@app.route('/api/save_schedule_to_db', methods=['POST'])
@login_required('admin')
def save_schedule_to_db():
    schedule_data = request.json
    if not schedule_data: 
        return jsonify({"status": "error", "msg": "没有接收到合法的编排名单数据"})
        
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)")
        
        c.execute("DELETE FROM start_list")
        c.execute("DELETE FROM system_settings WHERE key = 'active_track_heat'")

        c.execute("DELETE FROM registrations WHERE event_name LIKE '%(决赛)%' AND (score = '' OR score IS NULL)")

        for item in schedule_data:
            g_name = item.get('groupName') or item.get('group_name') or ''
            e_name = item.get('eventName') or item.get('event_name') or ''
            team_name = item.get('teamName') or item.get('team_name') or ''
            gender = item.get('gender') or ''
            heat = str(item.get('heat') or '1')
            lane = str(item.get('lane') or '1')
            bib = item.get('bib') or ''
            name = item.get('name') or ''
            evt_type = item.get('type') or 'sprint'
            
            total_lanes = int(item.get('totalLanes') or item.get('total_lanes') or 8)
            est_time = item.get('estTime') or item.get('est_time') or ''
            time_index = int(item.get('timeIndex') or item.get('time_index') or 0)
            
            is_field_val = item.get('isField') or item.get('is_field')
            is_field = 1 if (is_field_val is True or str(is_field_val).lower() in ['true', '1']) else 0

            c.execute('''
                INSERT INTO start_list 
                (group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field, score, checked_in, is_started, attempts_json) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, 0, '')
            ''', (g_name, e_name, gender, heat, lane, bib, name, team_name, evt_type, total_lanes, est_time, time_index, is_field))
            
        conn.commit()
        return jsonify({"status": "success", "msg": "✅ 新赛程发布成功！所有旧成绩、积分与决赛名单已联动重置为新赛事状态。"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": "写入数据库失败: " + str(e)})
    finally:
        conn.close()

@app.route('/api/get_referee_meta')
def get_referee_meta():
    conn = get_db_connection()
    try:
        c = conn.cursor()
        event_type_rows = c.execute("SELECT name, type, is_fun FROM cfg_events").fetchall()
        event_type_map = {}
        for r in event_type_rows:
            e_type = r['type'] or '径赛'
            if str(r['is_fun']) == '1' or '趣味' in str(e_type):
                e_type = '趣味'
            event_type_map[r['name']] = e_type
            pure_name = re.sub(r'[\(（].*?[\)）]', '', r['name']).strip()
            event_type_map[pure_name] = e_type

        rows = c.execute("""
            SELECT DISTINCT group_name, gender, event_name 
            FROM start_list 
            WHERE event_name != '' AND group_name != ''
            UNION
            SELECT DISTINCT r.group_name, r.gender, r.event_name
            FROM registrations r
            JOIN cfg_events e ON (r.event_name = e.name OR e.name LIKE '%' || r.event_name || '%')
            WHERE e.type = '趣味' OR e.is_fun = '1'
            ORDER BY group_name, gender, event_name
        """).fetchall()
    finally:
        conn.close()
    
    data = {}
    for r in rows:
        g, gen, e = r['group_name'], r['gender'], r['event_name']
        if not g or not gen or not e: continue
        if g not in data: data[g] = {}
        if gen not in data[g]: data[g][gen] = []
        
        clean_core = re.sub(r'[\(（].*?[\)）]', '', e).strip()
        matched_type = event_type_map.get(e) or event_type_map.get(clean_core)
        
        if not matched_type:
            if any(k in e for k in ['跳', '投', '铅球', '实心球', '标枪', '铁饼']):
                matched_type = '田赛'
            elif any(k in e for k in ['趣味', '沙包', '毽', '拔河', '毛毛虫', '障碍']):
                matched_type = '趣味'
            else:
                matched_type = '径赛'
        
        data[g][gen].append({
            "name": e,
            "type": matched_type
        })
        
    return jsonify(data)

@app.route('/api/get_event_start_list', methods=['POST'])
def get_event_start_list():
    data = request.json or {}
    event_name = str(data.get('event_name') or '').strip()
    group_name = str(data.get('group_name') or '').strip()
    gender = str(data.get('gender') or '').strip()

    clean_core = re.sub(r"\(.*?\)|（.*?）|预赛|决赛|男子|女子|混合", "", event_name).strip()
    norm_core = clean_core.replace('*', '×').replace('x', '×').replace('X', '×')

    conn = get_db_connection(readonly=False)
    c = conn.cursor()
    try:
        # 1. 规程配置查询
        cfg_row = c.execute("""
            SELECT type, is_fun, has_prelim, qualify_count 
            FROM cfg_events 
            WHERE name = ? OR name = ? OR name LIKE ?
            LIMIT 1
        """, (event_name, clean_core, f"%{clean_core}%")).fetchone()

        has_prelim = False
        is_fun = False
        default_qualify_count = 8
        if cfg_row:
            has_prelim = (to_bool_str(cfg_row['has_prelim']) == '1')
            is_fun = (cfg_row['type'] in ['趣味', '趣味项目', '健康测试'] or to_bool_str(cfg_row['is_fun']) == '1')
            if cfg_row['qualify_count'] is not None and str(cfg_row['qualify_count']).isdigit():
                default_qualify_count = int(cfg_row['qualify_count'])

        # 2. 读取各组别自定义录取名次映射表
        gq_map = {}
        try:
            gq_row = c.execute("SELECT value FROM sys_config WHERE key = 'group_qualify_map'").fetchone()
            if gq_row and gq_row['value']:
                gq_map = json.loads(gq_row['value'])
        except Exception:
            pass

        # 3. 趣味项目保底直出（若 start_list 无编排道次）
        if is_fun:
            count_in_start = c.execute(
                "SELECT COUNT(*) FROM start_list WHERE event_name = ? AND (? = '' OR group_name = ?)", 
                (event_name, group_name, group_name)
            ).fetchone()[0]
            if count_in_start == 0:
                rows = c.execute("""
                    SELECT 
                        id, id AS reg_id, group_name, event_name, gender, '1' AS heat, '' AS lane, 
                        bib, name, team_name, 'fun' AS type, 8 AS total_lanes, '' AS est_time, 
                        0 AS time_index, 0 AS is_field, 0 AS checked_in, 
                        score, score AS start_score, rank, points, record_bonus
                    FROM registrations
                    WHERE (event_name = ? OR event_name LIKE ? || '%')
                      AND (? = '' OR group_name = ?)
                      AND (? = '' OR gender = ?)
                """, (event_name, clean_core, group_name, group_name, gender, gender)).fetchall()
                result = [dict(r) for r in rows]
                for it in result:
                    it_g = it.get('group_name', '')
                    q_c = default_qualify_count
                    for test_k in [f"{it_g}_{norm_core}", f"{it_g}_{clean_core}", f"{it_g}_{event_name}"]:
                        if test_k in gq_map:
                            q_c = int(gq_map[test_k])
                            break
                    it['has_prelim'] = has_prelim
                    it['qualify_count'] = q_c
                return jsonify(result)

        # 4. 标准联合查询（参数数量：7 个，完全匹配 WHERE 条件里的 7 个问号）
        sql = """
            SELECT 
                s.id, 
                s.group_name, 
                s.event_name, 
                s.gender, 
                s.heat, 
                s.lane, 
                s.bib, 
                s.name, 
                s.team_name,
                s.time_index, 
                s.est_time, 
                s.is_field, 
                s.checked_in, 
                s.score AS start_score,
                COALESCE(s.points, r.points, 0) AS points,
                r.id AS reg_id, 
                r.score AS reg_score, 
                COALESCE(NULLIF(r.rank, ''), '') AS rank,
                COALESCE(r.record_bonus, 0) AS record_bonus,
                COALESCE(r.attempts_json, s.attempts_json, '') AS attempts_json
            FROM start_list s
            LEFT JOIN registrations r 
                ON TRIM(IFNULL(s.team_name, '')) = TRIM(IFNULL(r.team_name, ''))
                AND TRIM(IFNULL(s.group_name, '')) = TRIM(IFNULL(r.group_name, ''))
                AND s.gender = r.gender
                AND (
                    r.event_name = s.event_name 
                    OR r.event_name = REPLACE(s.event_name, ' (决赛)', '')
                    OR r.event_name = REPLACE(s.event_name, ' (预赛)', '')
                )
                AND (
                    s.event_name LIKE '%4x%' 
                    OR s.event_name LIKE '%4×%' 
                    OR s.event_name LIKE '%接力%'
                    OR s.name = r.name
                )
            WHERE (? = '' OR s.group_name = ?)
              AND (? = '' OR s.gender = ?)
              AND (
                  s.event_name = ? 
                  OR s.event_name LIKE ? || ' (场地%)'
                  OR s.event_name LIKE ? || '（场地%）'
              )
            GROUP BY s.id
            ORDER BY s.group_name ASC, s.gender ASC, CAST(s.heat AS INTEGER) ASC, CAST(s.lane AS INTEGER) ASC
        """
        params = (group_name, group_name, gender, gender, event_name, event_name, event_name)
        rows = c.execute(sql, params).fetchall()

        result = []
        for r in rows:
            item = dict(r)
            it_g = item.get('group_name', '')
            
            row_qualify_count = default_qualify_count
            for test_k in [f"{it_g}_{norm_core}", f"{it_g}_{clean_core}", f"{it_g}_{event_name}"]:
                if test_k in gq_map:
                    row_qualify_count = int(gq_map[test_k])
                    break

            item['has_prelim'] = has_prelim
            item['qualify_count'] = row_qualify_count
            
            raw_start = str(item.get('start_score') or '').strip()
            raw_reg = str(item.get('reg_score') or '').strip()

            if raw_start and raw_start != '-' and not any(k in raw_start for k in ['弃权', 'DNS', 'DQ']):
                item['score'] = raw_start
            elif raw_reg and raw_reg != '-' and not any(k in raw_reg for k in ['弃权', 'DNS', 'DQ']):
                item['score'] = raw_reg
            else:
                item['score'] = raw_start if raw_start else raw_reg

            if not item.get('reg_id'):
                item['reg_id'] = item.get('id')

            result.append(item)

        return jsonify(result)
    except Exception as e:
        print(f"❌ 获取道次列表异常: {e}")
        return jsonify({"status": "error", "msg": str(e)}), 500
    finally:
        conn.close()
def check_and_auto_promote_finals(group_name, raw_event_name, gender, c_cursor):
    """
    全自动晋级：严格区分田赛/径赛排序，彻底清理占位数据，生成真实决赛名单
    """
    clean_core = re.sub(r"\(.*?\)|（.*?）|预赛|决赛|男子|女子|混合|男|女", "", raw_event_name).strip()
    
    cfg_row = c_cursor.execute("""
        SELECT type, has_prelim, qualify_count FROM cfg_events 
        WHERE name = ? OR name = ? OR name LIKE ?
        LIMIT 1
    """, (clean_core, raw_event_name, f"%{clean_core}%")).fetchone()
    
    if not cfg_row or to_bool_str(cfg_row['has_prelim']) != '1':
        return False
        
    evt_type = str(cfg_row['type'] or '')
    is_field = '田' in evt_type or any(k in clean_core for k in ['跳远', '跳高', '实心球', '铅球', '标枪', '铁饼', '三级跳', '立定跳远'])
    
    top_n = int(cfg_row['qualify_count'] or 8)
    prelim_event_name = f"{clean_core} (预赛)"
    final_event_name = f"{clean_core} (决赛)"
    
    # 检查是否已人工发布过该决赛
    manual_chk = c_cursor.execute("""
        SELECT COUNT(*) FROM start_list 
        WHERE group_name = ? AND gender = ? 
          AND (event_name = ? OR event_name = ? || ' (决赛)' OR event_name = ? || '（决赛）') 
          AND is_manual_final = 1
    """, (group_name, gender, final_event_name, clean_core, clean_core)).fetchone()[0]
    if manual_chk > 0:
        return False
    finals_started_or_scored = c_cursor.execute("""
        SELECT COUNT(*) FROM start_list 
        WHERE group_name = ? AND gender = ? 
          AND (event_name = ? OR event_name LIKE ? || '%决赛%') 
          AND (
              is_started = 1 
              OR (score IS NOT NULL AND TRIM(score) != '' AND score != '-')
              OR checked_in = 2
          )
    """, (group_name, gender, final_event_name, clean_core)).fetchone()[0]
    if finals_started_or_scored > 0:
        # 决赛已开赛或已有选手录分，严禁自动清空覆写！
        return False
    # 检查预赛是否全员跑完（排除决赛占位数据和待定数据）
    prelim_runners = c_cursor.execute("""
        SELECT s.id, s.checked_in, IFNULL(NULLIF(TRIM(s.score), ''), IFNULL(r.score, '')) as score
        FROM start_list s
        LEFT JOIN registrations r 
            ON s.name = r.name AND s.team_name = r.team_name AND s.group_name = r.group_name AND s.gender = r.gender
            AND (r.event_name = s.event_name OR r.event_name = REPLACE(s.event_name, ' (预赛)', '') OR r.event_name = ?)
        WHERE s.group_name = ? AND s.gender = ? 
          AND (s.event_name = ? OR s.event_name = ? || ' (预赛)' OR s.event_name LIKE ? || '%预赛%')
          AND s.name NOT LIKE '%待定%' AND s.team_name != '预赛出线'
    """, (clean_core, group_name, gender, clean_core, clean_core, clean_core)).fetchall()

    if not prelim_runners: return False

    def is_finished(r):
        sc = str(r['score'] or '').strip()
        return int(r['checked_in'] or 0) == 2 or any(k in sc.upper() for k in ['弃权', 'DNS', 'DNF', 'DQ']) or (sc != '' and sc != '-')

    if not all(is_finished(r) for r in prelim_runners): return False

    # 提取预赛有效成绩排序（从 start_list 与 registrations 双表提取并去重）
    ath_rows = c_cursor.execute("""
        SELECT s.name, s.team_name, s.group_name, s.gender, s.bib,
               IFNULL(NULLIF(TRIM(s.score), ''), IFNULL(r.score, '')) AS score,
               COALESCE(r.team_id, t.id, 0) AS team_id
        FROM start_list s
        LEFT JOIN registrations r 
            ON s.name = r.name AND s.team_name = r.team_name AND s.group_name = r.group_name AND s.gender = r.gender
            AND (r.event_name = s.event_name OR r.event_name = REPLACE(s.event_name, ' (预赛)', '') OR r.event_name = ?)
        LEFT JOIN cfg_teams t ON s.team_name = t.name
        WHERE s.group_name = ? AND s.gender = ? 
          AND (s.event_name = ? OR s.event_name = ? || ' (预赛)' OR s.event_name LIKE ? || '%预赛%')
          AND s.name NOT LIKE '%待定%' AND s.team_name != '预赛出线'
    """, (clean_core, group_name, gender, clean_core, clean_core, clean_core)).fetchall()

    seen_athletes = set()
    valid_athletes = []
    for r in ath_rows:
        item = dict(r)
        sc = str(item['score'] or '').strip()
        if any(k in sc.upper() for k in ['弃权', 'DNS', 'DNF', 'DQ']):
            continue
        val = parse_time_to_seconds(sc)
        if val > 0:
            ath_key = (item['name'], item['team_name'])
            if ath_key not in seen_athletes:
                seen_athletes.add(ath_key)
                valid_athletes.append(item)

    if not valid_athletes: return False
    valid_athletes.sort(key=lambda x: parse_time_to_seconds(x['score']), reverse=is_field)

    # 🌟 关键拦截：如果第 top_n 名与第 top_n+1 名完全同分，停止盲目自动发布并告警
    if len(valid_athletes) > top_n:
        cutoff_val = parse_time_to_seconds(valid_athletes[top_n - 1]['score'])
        next_val = parse_time_to_seconds(valid_athletes[top_n]['score'])
        if abs(cutoff_val - next_val) < 0.0001:
            tied_runners = [a for a in valid_athletes if abs(parse_time_to_seconds(a['score']) - cutoff_val) < 0.0001]
            tied_names_str = "、".join([f"{a['name']}({a['team_name']})" for a in tied_runners])
            if 'socketio' in globals() and socketio:
                socketio.emit('finals_tie_warning', {
                    'group_name': group_name,
                    'event_name': final_event_name,
                    'gender': gender,
                    'top_n': top_n,
                    'score': valid_athletes[top_n - 1]['score'],
                    'tied_names': tied_names_str
                })
            return False  # 暂缓自动发布

    finalists = valid_athletes[:top_n]

    # 🌟 接入世界田联最新 TR 20.4 决赛道次同档抽签排定
    if is_field:
        lane_order = list(range(1, len(finalists) + 1))  # 田赛按次序 1, 2, 3... 出场
    else:
        lane_order = allocate_world_athletics_lanes(clean_core, len(finalists), top_n)

    existing_slot = c_cursor.execute("""
        SELECT est_time, time_index, total_lanes, type FROM start_list 
        WHERE group_name = ? AND gender = ? 
          AND (event_name = ? OR event_name LIKE ? || '%决赛%')
        ORDER BY id ASC LIMIT 1
    """, (group_name, gender, final_event_name, clean_core)).fetchone()

    base_est = existing_slot['est_time'] if existing_slot and existing_slot['est_time'] else '第2天下午 14:30'
    base_tidx = existing_slot['time_index'] if existing_slot and existing_slot['time_index'] else 800
    slot_type = existing_slot['type'] if existing_slot and existing_slot['type'] else ('field' if is_field else 'sprint')
    slot_lanes = existing_slot['total_lanes'] if existing_slot and existing_slot['total_lanes'] else 8
    
    g_row = c_cursor.execute("SELECT id FROM cfg_groups WHERE name = ?", (group_name,)).fetchone()
    gid = g_row['id'] if g_row else 0
    
    c_cursor.execute("""
        DELETE FROM registrations 
        WHERE group_name=? AND gender=? 
          AND (event_name=? OR event_name = ? || ' (决赛)' OR event_name = ? || '（决赛）')
    """, (group_name, gender, final_event_name, clean_core, clean_core))
    c_cursor.execute("""
        DELETE FROM start_list 
        WHERE group_name=? AND gender=? 
          AND (event_name=? OR event_name = ? || ' (决赛)' OR event_name = ? || '（决赛）')
    """, (group_name, gender, final_event_name, clean_core, clean_core))

    # 按田联最新抽签道次填入真实晋级名单
    for idx, ath in enumerate(finalists):
        assigned_lane = str(lane_order[idx])
        c_cursor.execute("""
            INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')
        """, (gid, group_name, ath.get('team_id', 0), ath.get('team_name', ''), ath.get('name', ''), gender, ath.get('bib', ''), final_event_name))
        
        c_cursor.execute("""
            INSERT INTO start_list (group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field, score, checked_in, is_started, is_manual_final)
            VALUES (?, ?, ?, '1', ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, 0, 0)
        """, (group_name, final_event_name, gender, assigned_lane, ath.get('bib', ''), ath.get('name', ''), ath.get('team_name', ''), slot_type, slot_lanes, base_est, base_tidx, 1 if is_field else 0))

    return True
@app.route('/api/batch_submit_field_scores', methods=['POST'])
@login_required('referee')
def batch_submit_field_scores():
    """田赛项目最后统一提交全部成绩并核算名次与积分"""
    data = request.json or {}
    athletes = data.get('athletes', [])
    if not athletes:
        return jsonify({"status": "error", "msg": "没有可提交的成绩数据"}), 400

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        event_name = ""
        group_name = ""

        for ath in athletes:
            reg_id = ath.get('reg_id') or ath.get('id')
            start_id = ath.get('id')
            name = ath.get('name', '')
            team_name = ath.get('team_name', '')
            group_name = ath.get('group_name', '')
            gender = ath.get('gender', '')
            event_name = ath.get('event_name', '')
            score_str = str(ath.get('score', '')).strip()
            attempts_json = ath.get('attempts_json', '[]')

            score_upper = score_str.upper()
            is_dq = any(k in score_upper for k in ['犯规', 'DQ', 'DISQ'])
            is_dns = any(k in score_upper for k in ['弃权', 'DNS', 'DNF'])
            is_abandoned = is_dq or is_dns
            chk_val = 2 if is_abandoned else 1

            # 1. 更新 start_list
            if start_id:
                c.execute("""
                    UPDATE start_list 
                    SET score = ?, attempts_json = ?, checked_in = ? 
                    WHERE id = ?
                """, (score_str, attempts_json, chk_val, start_id))
            
            # 2. 更新 registrations
            c.execute("""
                UPDATE registrations 
                SET score = ?, attempts_json = ? 
                WHERE name = ? AND team_name = ? AND group_name = ? AND gender = ?
                  AND (event_name = ? OR event_name = REPLACE(?, ' (预赛)', '') OR event_name LIKE ? || '%')
            """, (score_str, attempts_json, name, team_name, group_name, gender, event_name, event_name, event_name.split(' ')[0]))

        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"status": "error", "msg": f"批量提交失败: {str(e)}"}), 500
    finally:
        try: conn.close()
        except Exception: pass

    # 🌟 统一提交完毕后，立即执行积分与名次重算！
    msg = do_recalculate()
    if event_name:
        invalidate_event_cache(event_name, group_name)
    if '_cache_store' in globals():
        _cache_store.clear()

    if 'socketio' in globals() and socketio:
        socketio.emit('score_updated', {'event_name': event_name, 'group_name': group_name, 'heat_completed': True})
        socketio.emit('rank_updated', {'group_name': group_name})

    return jsonify({"status": "success", "msg": f"✅ 田赛成绩已全部统一提交并完成积分核算！\n({msg})"})
@app.route('/api/submit_score', methods=['POST'])
@login_required('referee')
def submit_score():
    data = request.json or {}
    incoming_id = data.get('id') or data.get('reg_id') or data.get('start_id')
    raw_val = str(data.get('score', '')).strip()
    
    is_dq = any(k in raw_val.upper() for k in ['犯规', 'DQ', 'DISQ'])
    is_dns = any(k in raw_val.upper() for k in ['弃权', 'DNS', 'DNF'])
    
    if is_dq:
        formatted_score = '犯规'
    elif is_dns:
        formatted_score = '弃权'
    else:
        clean_s = raw_val.replace('：', ':').replace('。', '.')
        if clean_s.count('.') >= 2:
            pts = clean_s.split('.')
            if len(pts) == 3: formatted_score = f"{pts[0]}:{pts[1]}.{pts[2]}"
            elif len(pts) == 4: formatted_score = f"{pts[0]}:{pts[1]}:{pts[2]}.{pts[3]}"
            else: formatted_score = clean_s
        else:
            formatted_score = clean_s

    is_abandoned = any(k in formatted_score.upper() for k in ['弃权', 'DNS', 'DNF', 'DQ'])
    if is_abandoned: 
        formatted_score = '弃权'

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        
        # 强制恢复检录状态：如果输入有效成绩，说明恢复比赛；输入弃权，设为2
        chk_val = 2 if is_abandoned else 1
        
        # 只更新数据，绝不调用 do_recalculate()
        c.execute("UPDATE start_list SET score = ?, checked_in = ? WHERE id = ?", (formatted_score, chk_val, incoming_id))
        
        st_row = c.execute("SELECT group_name, event_name, gender, heat, team_name, name FROM start_list WHERE id = ?", (incoming_id,)).fetchone()
        if st_row:
            group_name = st_row['group_name']
            event_name = st_row['event_name']
            gender = st_row['gender']
            heat = str(st_row['heat'])
            name = st_row['name']
            team_name = st_row['team_name']

            # 同步更新报名表
            if team_name:
                is_relay = any(k in event_name for k in ['接力', '4x', '4×', '4*'])
                if is_relay:
                    c.execute("""
                        UPDATE registrations 
                        SET score = ? 
                        WHERE team_name = ? AND group_name = ? AND gender = ?
                          AND (event_name = ? OR event_name LIKE ? || '%')
                    """, (formatted_score, team_name, group_name, gender, event_name, event_name.split(' ')[0]))
                elif name:
                    c.execute("""
                        UPDATE registrations 
                        SET score = ? 
                        WHERE name = ? AND team_name = ? AND group_name = ? AND gender = ?
                          AND (event_name = ? OR event_name = ? || ' (预赛)' OR event_name = REPLACE(?, ' (预赛)', ''))
                    """, (formatted_score, name, team_name, group_name, gender, event_name, event_name, event_name))

            # 统计该组是否已经录完（仅用于前端 UI 提示，不影响后台逻辑）
            unscored_in_heat = c.execute("""
                SELECT COUNT(*) FROM start_list 
                WHERE group_name = ? AND event_name = ? AND gender = ? AND heat = ?
                  AND (score IS NULL OR TRIM(score) = '' OR score = '-')
                  AND checked_in != 2
                  AND name != '' AND name IS NOT NULL
                  AND name NOT LIKE '%待定%'
            """, (group_name, event_name, gender, heat)).fetchone()[0]
            
            is_heat_fully_completed = (unscored_in_heat == 0)

            # 如果全组跑完，释放跑道占用（不重算）
            if is_heat_fully_completed:
                try: c.execute("DELETE FROM system_settings WHERE key = 'active_track_heat'")
                except Exception: pass

            conn.commit()
            # 只清除单项成绩的缓存，保证重新拉取时能看到最新数据
            invalidate_event_cache(event_name, group_name)
            
            return jsonify({
                "status": "success", 
                "msg": "成绩已保存", 
                "new_score": formatted_score,
                "heat_completed": is_heat_fully_completed
            })
        else:
            conn.rollback()
            return jsonify({"status": "error", "msg": "未找到对应的道次记录"})
            
    except Exception as e:
        if 'conn' in locals() and conn: conn.rollback()
        return jsonify({"status": "error", "msg": f"保存失败: {str(e)}"}), 500
    finally:
        if 'conn' in locals() and conn: conn.close()

@app.route('/api/settle_field_top8', methods=['POST'])
@login_required('referee')
def settle_field_top8():
    """后端统一按田径规则（含次优成绩决胜）计算前3轮前8名并按倒序返回"""
    data = request.json or {}
    event_name = data.get('event_name', '')
    group_name = data.get('group_name', '')
    gender = data.get('gender', '')

    conn = get_db_connection()
    c = conn.cursor()
    try:
        rows = c.execute("""
            SELECT id, group_name, event_name, gender, heat, lane, bib, name, team_name, score, attempts_json
            FROM start_list
            WHERE group_name = ? AND gender = ? AND (event_name = ? OR event_name LIKE ? || '%')
        """, (group_name, gender, event_name, event_name.split(' ')[0])).fetchall()

        valid_list = []
        for r in rows:
            item = dict(r)
            atts = []
            try:
                atts = json.loads(item['attempts_json']) if item['attempts_json'] else []
            except Exception:
                atts = []
            while len(atts) < 6:
                atts.append('')
            # 仅取前 3 轮有效成绩参与前 8 强选拔
            first_3_key = parse_field_attempts_tie_breaker('', json.dumps(atts[:3]))
            if first_3_key[0] > 0:
                item['_key'] = first_3_key
                item['prelimBest'] = first_3_key[0]
                item['attempts'] = atts
                valid_list.append(item)

        if not valid_list:
            return jsonify({"status": "error", "msg": "前3轮尚未录入任何有效成绩，无法截取！"})

        # 按田径规则降序排列，取前8名，再倒序出场
        valid_list.sort(key=lambda x: x['_key'], reverse=True)
        top8 = valid_list[:8]
        top8.reverse()
        for idx, item in enumerate(top8):
            item.pop('_key', None)
            item['isFinalist'] = True
            item['orderIndex'] = idx + 1

        return jsonify({"status": "success", "top8": top8})
    finally:
        conn.close()
@app.route('/api/publish_finals', methods=['POST'])
@login_required('admin')
def publish_finals():
    data = request.json or {}
    display_name = data.get('final_event_name')
    g_name = data.get('group_name')
    gender = data.get('gender')
    athletes = data.get('athletes', [])

    if not athletes: 
        return jsonify({"status": "error", "msg": "决赛出线名单为空"})

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        
        g_info = c.execute("SELECT id FROM cfg_groups WHERE name=?", (g_name,)).fetchone()
        gid = g_info['id'] if g_info else 0

        clean_core = re.sub(r'\(.*?\)|（.*?）|决赛|预赛', '', display_name).strip()
        
        # 1. 查询原沙盘中是否已排定该决赛的槽位
        existing_slots = c.execute("""
            SELECT id, est_time, time_index, total_lanes, type, is_field, heat, lane
            FROM start_list 
            WHERE group_name = ? AND gender = ? 
              AND (event_name = ? OR event_name = ? OR event_name LIKE ? || '%决赛%')
            ORDER BY CAST(heat AS INTEGER) ASC, CAST(lane AS INTEGER) ASC
        """, (g_name, gender, display_name, f"{clean_core} (决赛)", clean_core)).fetchall()

        # 先清理旧报名与旧决赛道次
        c.execute("DELETE FROM registrations WHERE group_name=? AND event_name=? AND gender=?", (g_name, display_name, gender))
        c.execute("""
    DELETE FROM start_list 
    WHERE group_name=? AND gender=? 
      AND (event_name=? OR event_name = ? || ' (决赛)' OR event_name = ? || '（决赛）')
""", (g_name, gender, display_name, clean_core, clean_core))

        if existing_slots and len(existing_slots) > 0:
            # 🌟 情况 A：沙盘中排过决赛，复用原沙盘预设的时间与索引
            base_est = existing_slots[0]['est_time'] or '第2天下午 14:30'
            base_tidx = existing_slots[0]['time_index'] or 100
            
            for i, ath in enumerate(athletes):
                lane = str(ath.get('finalLane', i + 1))
                heat = '1'
                
                c.execute("""INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, score)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')""", 
                          (gid, g_name, ath.get('team_id', 0), ath.get('team_name', ''), ath.get('name', ''), gender, ath.get('bib', ''), display_name))
                
                # 写入带 is_manual_final = 1 人工锁定标记
                c.execute("""INSERT INTO start_list (group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field, score, checked_in, is_started, is_manual_final)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sprint', 8, ?, ?, 0, '', 0, 0, 1)""",
                          (g_name, display_name, gender, heat, lane, ath.get('bib', ''), ath.get('name', ''), ath.get('team_name', ''), base_est, base_tidx))
        else:
            # 🌟 情况 B：沙盘中未排决赛（即缺少的那段），根据预赛结束时间自动推算并创建决赛
            prelim_meta = c.execute("""
                SELECT est_time, time_index FROM start_list 
                WHERE group_name=? AND gender=? AND (event_name LIKE ? || '%' OR event_name=?) 
                ORDER BY time_index DESC LIMIT 1
            """, (g_name, gender, clean_core, clean_core)).fetchone()
            
            base_est = prelim_meta['est_time'] if prelim_meta and prelim_meta['est_time'] else '第2天下午 14:30'
            base_tidx = (prelim_meta['time_index'] + 50) if prelim_meta and prelim_meta['time_index'] is not None else 500

            for i, ath in enumerate(athletes):
                lane = str(ath.get('finalLane', i + 1))
                c.execute("""INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, score)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')""", 
                          (gid, g_name, ath.get('team_id', 0), ath.get('team_name', ''), ath.get('name', ''), gender, ath.get('bib', ''), display_name))
                
                # 写入带 is_manual_final = 1 人工锁定标记
                c.execute("""INSERT INTO start_list (group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field, score, checked_in, is_started, is_manual_final)
                             VALUES (?, ?, ?, '1', ?, ?, ?, ?, 'sprint', 8, ?, ?, 0, '', 0, 0, 1)""",
                          (g_name, display_name, gender, lane, ath.get('bib', ''), ath.get('name', ''), ath.get('team_name', ''), base_est, base_tidx))

        conn.commit()
        
        # 刷新缓存与 WebSocket 通知
        invalidate_event_cache(display_name, g_name)
        if '_cache_store' in globals():
            _cache_store.pop("get_data_public", None)
            _cache_store.pop("query_nav_events", None)

        if 'socketio' in globals() and socketio:
            socketio.emit('score_updated', {'event_name': clean_core, 'group_name': g_name})
            socketio.emit('score_updated', {'event_name': display_name, 'group_name': g_name})

        return jsonify({"status": "success", "msg": "✅ 决赛名单已人工更正锁定并发布！裁判端与查询端已同步更新。"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)}), 500
    finally:
        conn.close()
@app.route('/api/manage_team_passwords', methods=['POST'])
@login_required('admin')
def manage_team_passwords():
    action = request.json.get('action') if request.json else None
    conn = get_db_connection()
    c = conn.cursor()

    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("""
            DELETE FROM team_auth 
            WHERE team_name NOT IN (SELECT name FROM cfg_teams WHERE name != '' AND name IS NOT NULL)
        """)
        if action == 'generate':
            current_teams = [r['name'] for r in c.execute("SELECT name FROM cfg_teams WHERE name != '' AND name IS NOT NULL").fetchall()]
            for team in current_teams:
                if not c.execute("SELECT 1 FROM team_auth WHERE team_name = ?", (team,)).fetchone():
                    new_pass = ''.join(random.choices(string.digits, k=6))
                    c.execute("INSERT INTO team_auth (team_name, password) VALUES (?, ?)", (team, new_pass))
        
        conn.commit()

        query = """
            SELECT 
                IFNULL(g.name, '未分配组别') as group_name, 
                t.name as team_name, 
                IFNULL(ta.password, '------') as password
            FROM cfg_teams t
            JOIN cfg_groups g ON t.group_id = g.id
            LEFT JOIN team_auth ta ON t.name = ta.team_name
            ORDER BY g.name ASC, t.name ASC
        """
        rows = c.execute(query).fetchall()
        return jsonify([{'group': r['group_name'], 'team': r['team_name'], 'password': r['password']} for r in rows])
    except Exception as e:
        conn.rollback()
        print(f"同步代表队密码库异常: {e}")
        return jsonify({"status": "error", "msg": str(e)}), 500
    finally:
        conn.close()
@app.route('/api/generate_finals_list', methods=['POST'])
@login_required('admin')
def generate_finals_list():
    data = request.json or {}
    g_name = data.get('group_name') 
    gender = data.get('gender')      
    base_evt = data.get('event', '') 
    top_n = int(data.get('top_n', 8))

    if '决赛' in base_evt:
        return jsonify({"status": "error", "msg": f"【{base_evt}】当前已是决赛，无法从决赛中再次提取决赛名单！"})

    conn = get_db_connection()
    c = conn.cursor()
    try:
        clean_core = re.sub(r"男子|女子|混合", "", base_evt).strip()
        clean_core = clean_core.replace(' (预赛)', '').replace(' (决赛)', '').replace('()', '').replace('（）', '').strip()

        row = c.execute("SELECT has_prelim FROM cfg_events WHERE name = ?", (clean_core,)).fetchone()
        if not row:
            row = c.execute("SELECT has_prelim FROM cfg_events WHERE name LIKE ?", (f"%{clean_core}%",)).fetchone()
        
        if row:
            is_prelim = (str(row['has_prelim']) == '1' or str(row['has_prelim']).lower() == 'true')
            if not is_prelim:
                return jsonify({"status": "error", "msg": f"【{clean_core}】是直接决赛项目，无需生成决赛表！"})

        query = """
            SELECT id, team_id, team_name, name, gender, bib, score 
            FROM registrations 
            WHERE group_name = ? 
              AND gender = ? 
              AND (event_name = ? OR event_name = ?)
              AND score != '' AND score IS NOT NULL
              AND score NOT LIKE '%弃权%'
              AND score NOT LIKE '%DNS%'
              AND score NOT LIKE '%DQ%'
              AND score NOT LIKE '%DNF%'
        """
        rows = c.execute(query, (g_name, gender, clean_core, f"{clean_core} (预赛)")).fetchall()
        
        athletes = [dict(r) for r in rows if parse_time_to_seconds(r['score']) > 0]
        
        if not athletes:
            return jsonify({"status": "error", "msg": f"未找到【{g_name} {gender}·{clean_core}】的有效完赛成绩，请确认是否所有选手都未完赛或已弃权！"})

        is_field = clean_core.endswith('跳远') or clean_core.endswith('跳高') or clean_core.endswith('铅球') or clean_core.endswith('实心球') or clean_core.endswith('标枪')
        athletes.sort(key=lambda x: parse_time_to_seconds(x['score']), reverse=is_field)
        final_display_name = f"{clean_core} (决赛)" 
        
        finalists = athletes[:top_n]
        force_redraw = data.get('force_redraw', False) # 是否为管理员主动点击重抽签

        # 🌟 1. 核心改进：先查 start_list 数据库是否已经有排定好的决赛道次（如已自动晋级）
        existing_slots = c.execute("""
            SELECT name, team_name, lane, bib
            FROM start_list
            WHERE group_name = ? AND gender = ? 
              AND (event_name = ? OR event_name = ? || ' (决赛)' OR event_name LIKE ? || '%决赛%')
              AND name NOT LIKE '%待定%' AND team_name != '预赛出线'
        """, (g_name, gender, final_display_name, clean_core, clean_core)).fetchall()

        existing_lane_map = { (r['name'], r['team_name']): int(r['lane']) for r in existing_slots if str(r['lane']).isdigit() }

        # 如果数据库已有决赛道次，且不是强制重抽签，100% 直接继承已有道次！
        if len(existing_lane_map) >= len(finalists) and all((ath['name'], ath['team_name']) in existing_lane_map for ath in finalists) and not force_redraw:
            for ath in finalists:
                ath['finalLane'] = existing_lane_map[(ath['name'], ath['team_name'])]
            rule_desc = "世界田联最新 TR 20.4 决赛道次同档抽签排定"
        else:
            # 数据库尚无道次，或管理员点击了“强制重新抽签”时，执行世界田联 TR 20.4 抽签
            if is_field:
                lane_order = list(range(1, len(finalists) + 1))
                rule_desc = "田赛出场顺序 (1-N)"
            else:
                lane_order = allocate_world_athletics_lanes(clean_core, len(finalists), 8)
                if '200' in clean_core or '300' in clean_core:
                    rule_desc = "WA TR 20.4.4: 1-3名抽签5-7道, 4-5名抽签4/8道, 6名分3道, 7-8名抽签1-2道"
                elif any(k in clean_core for k in ['400', '接力', '4×', '4x']):
                    rule_desc = "WA TR 20.4.5: 1-4名抽签4-7道, 5-6名抽签3/8道, 7-8名抽签1-2道"
                else:
                    rule_desc = "WA TR 20.4.3: 1-4名抽签3-6道, 5-6名抽签2/7道, 7-8名抽签1/8道"

            for idx, ath in enumerate(finalists):
                ath['finalLane'] = lane_order[idx]

        # 检查第 8 名并列预警
        has_tie = False
        tied_info = []
        if len(athletes) > top_n:
            cutoff = parse_time_to_seconds(athletes[top_n - 1]['score'])
            next_score = parse_time_to_seconds(athletes[top_n]['score'])
            if abs(cutoff - next_score) < 0.0001:
                has_tie = True
                tied_info = [{"name": a['name'], "team": a['team_name'], "score": a['score']} for a in athletes if abs(parse_time_to_seconds(a['score']) - cutoff) < 0.0001]

        return jsonify({
            "status": "success",
            "final_event_name": final_display_name, 
            "group_name": g_name,
            "gender": gender,
            "athletes": finalists,
            "has_tie": has_tie,
            "tied_athletes": tied_info,
            "top_n": top_n,
            "rule_desc": rule_desc
        })
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()

# 导入导出接口
@app.route('/api/export_system')
@login_required('admin')
def export_system():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        # 读取起止日期
        s_row = c.execute("SELECT value FROM system_settings WHERE key='start_date'").fetchone()
        e_row = c.execute("SELECT value FROM system_settings WHERE key='end_date'").fetchone()

        data = {
            "version": "2026_full",
            "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "start_date": s_row[0] if s_row else "",
            "end_date": e_row[0] if e_row else "",
            "groups": [dict(r) for r in c.execute("SELECT * FROM cfg_groups").fetchall()],
            "teams": [dict(r) for r in c.execute("SELECT * FROM cfg_teams").fetchall()],
            "events": [dict(r) for r in c.execute("SELECT * FROM cfg_events").fetchall()],
            "config": {r['key']: r['value'] for r in c.execute("SELECT * FROM sys_config").fetchall()},
            "system_settings": [dict(r) for r in c.execute("SELECT * FROM system_settings").fetchall()],
            "team_auth": [dict(r) for r in c.execute("SELECT * FROM team_auth").fetchall()],
            "group_records": [dict(r) for r in c.execute("SELECT * FROM cfg_group_records").fetchall()],
            "registrations": [dict(r) for r in c.execute("SELECT * FROM registrations").fetchall()],
            "start_list": [dict(r) for r in c.execute("SELECT * FROM start_list").fetchall()]
        }
        mem = BytesIO()
        mem.write(json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
        mem.seek(0)
        return send_file(
            mem, 
            mimetype='application/json', 
            as_attachment=True, 
            download_name=f'田径运动会全量数据备份_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        )
    finally:
        conn.close()
@app.route('/api/import_system', methods=['POST'])
@login_required('admin')
def import_system():
    if 'file' not in request.files: 
        return jsonify({"status": "error", "msg": "未检测到上传的备份文件！"}), 400
    file = request.files['file']
    if not file or file.filename == '':
        return jsonify({"status": "error", "msg": "上传文件为空！"}), 400

    conn = None
    try:
        data = json.load(file)
        conn = get_db_connection()
        c = conn.cursor()
        
        # 1. 设置长超时并临时关闭刷盘等待（绝不切换 journal_mode，保持 WAL 模式）
        c.execute("PRAGMA busy_timeout = 60000;")
        c.execute("PRAGMA synchronous = OFF;")
        
        # 2. 确保目标表结构存在（直接用当前连接建表，包含所有最新字段）
        c.execute('''CREATE TABLE IF NOT EXISTS cfg_groups (id INTEGER PRIMARY KEY, name TEXT, prefix TEXT, sort_order INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS cfg_teams (id INTEGER PRIMARY KEY, group_id INTEGER, name TEXT, leader TEXT, coach TEXT DEFAULT '', phone TEXT DEFAULT '', sort_order INTEGER DEFAULT 0, custom_scores TEXT DEFAULT '{}')''')
        c.execute('''CREATE TABLE IF NOT EXISTS cfg_events (id INTEGER PRIMARY KEY, name TEXT, type TEXT, gender TEXT, score_rule TEXT, record TEXT, record_bonus TEXT, is_double_score BOOLEAN, need_lane BOOLEAN, has_prelim BOOLEAN, is_relay BOOLEAN, is_team TEXT DEFAULT '0', limit_count INTEGER, allowed_groups TEXT DEFAULT '', duration REAL DEFAULT 5, venue_count INTEGER DEFAULT 1, is_fun TEXT DEFAULT '0', qualify_count INTEGER DEFAULT 8, dense_rank TEXT DEFAULT '0')''')
        c.execute('''CREATE TABLE IF NOT EXISTS sys_config (key TEXT PRIMARY KEY, value TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS team_auth (id INTEGER PRIMARY KEY AUTOINCREMENT, team_name TEXT, password TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS cfg_group_records (id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT, event_name TEXT, gender TEXT, records_json TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS registrations (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, group_name TEXT, team_id INTEGER, team_name TEXT, name TEXT, gender TEXT, bib TEXT, event_name TEXT, score TEXT DEFAULT '', rank TEXT DEFAULT '', lane TEXT DEFAULT '', heat TEXT DEFAULT '', submit_time TEXT, points INTEGER DEFAULT 0, record_bonus INTEGER DEFAULT 0, attempts_json TEXT DEFAULT '', relay_leg TEXT DEFAULT '')''')
        c.execute('''CREATE TABLE IF NOT EXISTS start_list (id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT, event_name TEXT, gender TEXT, heat TEXT, lane TEXT, bib TEXT, name TEXT, team_name TEXT, type TEXT, total_lanes INTEGER DEFAULT 8, est_time TEXT DEFAULT '', time_index INTEGER DEFAULT 0, is_field INTEGER DEFAULT 0, score TEXT DEFAULT '', checked_in INTEGER DEFAULT 0, is_started INTEGER DEFAULT 0, attempts_json TEXT DEFAULT '')''')

        # 🌟 补齐旧表可能缺失的所有新字段，避免导入报错
        for tbl, col, dtype in [
            ("cfg_groups", "prefix", "TEXT DEFAULT '-'"),
            ("cfg_groups", "sort_order", "INTEGER DEFAULT 0"),
            ("cfg_teams", "leader", "TEXT DEFAULT ''"),
            ("cfg_teams", "coach", "TEXT DEFAULT ''"),
            ("cfg_teams", "phone", "TEXT DEFAULT ''"),
            ("cfg_teams", "sort_order", "INTEGER DEFAULT 0"),
            ("cfg_teams", "custom_scores", "TEXT DEFAULT '{}'"),
            ("cfg_events", "qualify_count", "INTEGER DEFAULT 8"),
            ("cfg_events", "dense_rank", "TEXT DEFAULT '0'"),
            ("cfg_events", "is_fun", "TEXT DEFAULT '0'"),
            ("cfg_events", "is_team", "TEXT DEFAULT '0'"),
            ("start_list", "is_manual_final", "INTEGER DEFAULT 0")
        ]:
            try: c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {dtype}")
            except Exception: pass

        # 3. 开启单连接事务
        c.execute("BEGIN IMMEDIATE;")

        # 4. 清理当前数据表
        for tbl in ["cfg_groups", "cfg_teams", "cfg_events", "sys_config", "system_settings", "team_auth", "cfg_group_records", "registrations", "start_list"]:
            c.execute(f"DELETE FROM {tbl}")

       # 5. 批量写入 cfg_groups（携带排序权重）
        if data.get('groups'):
            groups_data = [
                (g.get('id'), g.get('name', ''), g.get('prefix', '-'), g.get('sort_order', idx))
                for idx, g in enumerate(data['groups'])
            ]
            c.executemany("INSERT OR REPLACE INTO cfg_groups (id, name, prefix, sort_order) VALUES (?, ?, ?, ?)", groups_data)

        # 6. 批量写入 cfg_teams（携带排序权重）
        if data.get('teams'):
            teams_data = [(
                t.get('id'), t.get('group_id') or t.get('groupId'), t.get('name', ''), 
                t.get('leader', ''), t.get('coach', ''), t.get('phone', ''),
                t.get('sort_order', idx),
                t.get('custom_scores') if isinstance(t.get('custom_scores'), str) else json.dumps(t.get('custom_scores') or {}, ensure_ascii=False)
            ) for idx, t in enumerate(data['teams'])]
            c.executemany("""
                INSERT OR REPLACE INTO cfg_teams (id, group_id, name, leader, coach, phone, sort_order, custom_scores) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, teams_data)

        # 7. 批量写入 cfg_events
        if data.get('events'):
            events_data = []
            for e in data['events']:
                # 严谨获取限额：如果是 0 则严格保留 0，只有为 None/空串时才回退默认值
                raw_lim = e.get('limit_count') if e.get('limit_count') is not None else e.get('limit')
                lim_val = int(raw_lim) if (raw_lim is not None and str(raw_lim).strip() != '') else 8

                raw_dur = e.get('duration')
                dur_val = float(raw_dur) if (raw_dur is not None and str(raw_dur).strip() != '') else 5.0

                raw_ven = e.get('venue_count') if e.get('venue_count') is not None else e.get('venueCount')
                ven_val = int(raw_ven) if (raw_ven is not None and str(raw_ven).strip() != '') else 1

                raw_qua = e.get('qualify_count') if e.get('qualify_count') is not None else e.get('qualifyCount')
                qua_val = int(raw_qua) if (raw_qua is not None and str(raw_qua).strip() != '') else 8

                events_data.append((
                    e.get('id'), e.get('name', ''), e.get('type', '径赛'), e.get('gender', '双性'),
                    e.get('score_rule') or e.get('scoreRule') or '9,7,6,5,4,3,2,1',
                    e.get('record', ''), e.get('record_bonus') or e.get('recordBonus') or 0,
                    to_bool_str(e.get('is_double_score') or e.get('isDoubleScore')),
                    to_bool_str(e.get('need_lane') or e.get('needLane')),
                    to_bool_str(e.get('has_prelim') or e.get('hasPrelim')),
                    to_bool_str(e.get('is_relay') or e.get('isRelay')),
                    to_bool_str(e.get('is_team') or e.get('isTeam')),
                    lim_val,
                    e.get('allowed_groups') or e.get('allowedGroups') or '',
                    dur_val, ven_val,
                    to_bool_str(e.get('is_fun') or e.get('isFun')),
                    qua_val,
                    to_bool_str(e.get('dense_rank') or e.get('denseRank'))
                ))

            c.executemany("""
                INSERT OR REPLACE INTO cfg_events 
                (id, name, type, gender, score_rule, record, record_bonus, 
                 is_double_score, need_lane, has_prelim, is_relay, is_team, limit_count, 
                 allowed_groups, duration, venue_count, is_fun, qualify_count, dense_rank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, events_data)

        # 8. 批量写入 sys_config
        cfg_dict = data.get('config', {})
        if isinstance(cfg_dict, dict) and cfg_dict:
            c.executemany("INSERT OR REPLACE INTO sys_config (key, value) VALUES (?, ?)", 
                          [(k, str(v)) for k, v in cfg_dict.items()])

        # 9. 批量写入 system_settings 并确保起止日期落盘
        if data.get('system_settings'):
            c.executemany("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                          [(s.get('key'), s.get('value')) for s in data['system_settings']])

        # 🌟 显式恢复顶级保存的起止日期
        if data.get('start_date'):
            c.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('start_date', ?)", (data['start_date'],))
        if data.get('end_date'):
            c.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('end_date', ?)", (data['end_date'],))

        # 10. 批量写入 team_auth
        if data.get('team_auth'):
            c.executemany("INSERT OR REPLACE INTO team_auth (id, team_name, password) VALUES (?, ?, ?)",
                          [(a.get('id'), a.get('team_name'), a.get('password')) for a in data['team_auth']])

        # 11. 批量写入 cfg_group_records
        if data.get('group_records'):
            c.executemany("INSERT OR REPLACE INTO cfg_group_records (id, group_name, event_name, gender, records_json) VALUES (?, ?, ?, ?, ?)",
                          [(gr.get('id'), gr.get('group_name'), gr.get('event_name'), gr.get('gender'), gr.get('records_json')) for gr in data['group_records']])

        # 12. 批量写入 registrations
        if data.get('registrations'):
            regs_data = [(
                r.get('id'), r.get('group_id'), r.get('group_name'), r.get('team_id'), r.get('team_name'),
                r.get('name'), r.get('gender'), r.get('bib'), r.get('event_name'),
                r.get('score', ''), r.get('rank', ''), r.get('lane', ''), r.get('heat', ''),
                r.get('submit_time', ''), r.get('points', 0), r.get('record_bonus', 0),
                r.get('attempts_json', ''), r.get('relay_leg', '')
            ) for r in data['registrations']]
            c.executemany("""
                INSERT OR REPLACE INTO registrations 
                (id, group_id, group_name, team_id, team_name, name, gender, bib, event_name, 
                 score, rank, lane, heat, submit_time, points, record_bonus, attempts_json, relay_leg)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, regs_data)

        # 13. 批量写入 start_list
        if data.get('start_list'):
            start_data = [(
                sl.get('id'), sl.get('group_name'), sl.get('event_name'), sl.get('gender'),
                sl.get('heat'), sl.get('lane'), sl.get('bib'), sl.get('name'), sl.get('team_name'),
                sl.get('type'), sl.get('total_lanes', 8), sl.get('est_time', ''), sl.get('time_index', 0),
                sl.get('is_field', 0), sl.get('score', ''), sl.get('checked_in', 0),
                sl.get('is_started', 0), sl.get('attempts_json', ''), sl.get('is_manual_final', 0)
            ) for sl in data['start_list']]
            c.executemany("""
                INSERT OR REPLACE INTO start_list 
                (id, group_name, event_name, gender, heat, lane, bib, name, team_name, type, 
                 total_lanes, est_time, time_index, is_field, score, checked_in, is_started, attempts_json, is_manual_final)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, start_data)

        # 14. 提交事务
        conn.commit()
        
        # 恢复正常的同步配置
        c.execute("PRAGMA synchronous = NORMAL;")
        _cache_store.clear()

        return jsonify({
            "status": "success", 
            "msg": f"🎉 备份已恢复成功！\n已极速导入 {len(data.get('registrations', []))} 条名单成绩与 {len(data.get('start_list', []))} 条赛程道次。"
        })
    except Exception as e:
        if conn:
            try: conn.rollback()
            except Exception: pass
        return jsonify({"status": "error", "msg": f"导入解析失败: {str(e)}"}), 500
    finally:
        if conn:
            conn.close()
@app.route('/api/export_registrations')
@login_required('admin')
def export_registrations():
    conn = get_db_connection()
    c = conn.cursor()
    try: 
        rows = c.execute("SELECT group_name, team_name, name, gender, bib, event_name FROM registrations").fetchall()
    except Exception as e: 
        return f"导出错误: {str(e)}"
    finally: 
        conn.close()

    athletes_map = {}
    max_event_count = 0
    for r in rows:
        g_name, t_name, name, gender, bib, evt = r
        key = f"{g_name}_{t_name}_{name}"
        if key not in athletes_map: 
            athletes_map[key] = {'group': g_name, 'team': t_name, 'name': name, 'gender': gender, 'bib': bib, 'events': []}
        if evt:
            athletes_map[key]['events'].append(evt)
            if len(athletes_map[key]['events']) > max_event_count: 
                max_event_count = len(athletes_map[key]['events'])

    if max_event_count < 3: max_event_count = 3
    output = StringIO()
    output.write('\ufeff')
    writer = csv.writer(output)
    headers = ['组别', '代表队', '姓名', '性别', '号码'] + [f'项目{i+1}' for i in range(max_event_count)]
    writer.writerow(headers)
    
    for p in athletes_map.values():
        row = [p['group'], p['team'], p['name'], p['gender'], p['bib']] + p['events']
        row.extend([''] * (max_event_count - len(p['events'])))
        writer.writerow(row)
        
    mem = BytesIO()
    mem.write(output.getvalue().encode('utf-8-sig'))
    mem.seek(0)
    return send_file(mem, mimetype='text/csv', as_attachment=True, download_name=f'报名名单_{datetime.now().strftime("%Y%m%d")}.csv')

@app.route('/api/import_registrations', methods=['POST'])
@login_required('admin')
def import_registrations():
    if 'file' not in request.files: 
        return jsonify({"status": "error", "msg": "未上传文件"})
    file = request.files['file']
    if not file.filename.endswith('.csv'): 
        return jsonify({"status": "error", "msg": "请上传 .csv 文件"})

    try:
        stream = StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
        csv_input = csv.reader(stream)
        next(csv_input, None)
        
        conn = get_db_connection() 
        c = conn.cursor()
        
        groups_map = {row['name']: row['id'] for row in c.execute("SELECT id, name FROM cfg_groups").fetchall()}
        teams_map = {row['name']: row['id'] for row in c.execute("SELECT id, name FROM cfg_teams").fetchall()}
        
        # 1. 读取所有项目的类型与趣味标记
        event_cfgs = {row['name']: dict(row) for row in c.execute("SELECT name, type, is_fun, is_relay, gender FROM cfg_events").fetchall()}
        
        sys_config = {row['key']: row['value'] for row in c.execute("SELECT key, value FROM sys_config").fetchall()}
        MAX_TOTAL = int(sys_config.get('maxTotal', 20))
        MAX_PER_EVENT = int(sys_config.get('maxPerEvent', 3))
        
        success_count = 0
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        c.execute("BEGIN IMMEDIATE")

        for row in csv_input:
            if len(row) < 4: continue 
            g_name, t_name, name, gender = row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip()
            bib = row[4].strip() if len(row) > 4 else ""
            
            gid = groups_map.get(g_name, 0)
            tid = teams_map.get(t_name, 0)
            
            if not gid or not tid: continue

            # 解析该学生报的所有项目
            event_list = [item.strip() for col in row[5:] for item in col.replace('，', ',').split(',') if item.strip()]
            unique_events = list(dict.fromkeys(event_list))
            if not unique_events: continue

            # 🌟 判断项目是否属于趣味或健康测试
            def is_fun_event(e_name):
                cfg = event_cfgs.get(e_name)
                if cfg:
                    e_type = str(cfg.get('type') or '')
                    return ('趣味' in e_type) or ('健康' in e_type) or (str(cfg.get('is_fun')) == '1')
                return any(k in e_name for k in ['趣味', '健康', '跳绳', '投篮', '垫球', '绕杆', '仰卧', '引体'])

            # 🌟 检查该学生是否包含常规田赛/径赛单项
            has_track_or_field = any(not is_fun_event(e) for e in unique_events)

            # 🌟 如果该学生包含田径竞技项目，才做班级总人数（MAX_TOTAL）拦截
            is_new_athlete = not c.execute("SELECT 1 FROM registrations WHERE team_id=? AND name=?", (tid, name)).fetchone()
            if is_new_athlete and has_track_or_field:
                # 统计当前队伍已经报了田径竞技项目的总人数
                curr_comp_total = c.execute("""
                    SELECT COUNT(DISTINCT r.name) 
                    FROM registrations r
                    WHERE r.team_id = ? 
                      AND r.event_name NOT IN (
                          SELECT name FROM cfg_events WHERE type IN ('趣味', '趣味项目', '健康测试') OR is_fun = '1'
                      )
                """, (tid,)).fetchone()[0]
                
                if curr_comp_total >= MAX_TOTAL:
                    # 如果田径名额已满，只允许报趣味/健康测试项目
                    unique_events = [e for e in unique_events if is_fun_event(e)]
                    if not unique_events:
                        continue

            # 遍历写入每个项目
            for sub_evt in unique_events:
                exists = c.execute("SELECT 1 FROM registrations WHERE team_id=? AND name=? AND event_name=?", (tid, name, sub_evt)).fetchone()
                if exists: continue
                
                is_fun = is_fun_event(sub_evt)
                
                # 仅对常规田径项目进行单项名额限制（每项每队限3人等）
                if not is_fun:
                    evt_meta = event_cfgs.get(sub_evt)
                    is_relay = (to_bool_str(evt_meta['is_relay']) == '1') if evt_meta else False
                    is_mixed = (evt_meta['gender'] == '混合') if evt_meta else False
                    
                    if is_mixed and is_relay:
                        current_limit = 10
                    elif is_relay:
                        current_limit = 4
                    else:
                        current_limit = MAX_PER_EVENT
                    
                    curr_evt_count = c.execute(
                        "SELECT COUNT(*) FROM registrations WHERE team_id=? AND event_name=? AND gender=?", 
                        (tid, sub_evt, gender)
                    ).fetchone()[0]
                    
                    if curr_evt_count >= current_limit: 
                        continue

                c.execute('''INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, submit_time) 
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
                          (gid, g_name, tid, t_name, name, gender, bib, sub_evt, now_str))
                success_count += 1

        conn.commit()
        # 🌟 清理缓存，确保前台立即显示真实统计
        if '_cache_store' in globals():
            _cache_store.clear()

        return jsonify({"status": "success", "msg": f"✅ 成功导入 {success_count} 条记录！"})
    except Exception as e:
        if 'conn' in locals(): conn.rollback()
        return jsonify({"status": "error", "msg": "导入失败: " + str(e)})
    finally:
        if 'conn' in locals(): conn.close()

# 数据库维护与补丁
def init_db():
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS cfg_groups (id INTEGER PRIMARY KEY, name TEXT, prefix TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS cfg_teams (id INTEGER PRIMARY KEY, group_id INTEGER, name TEXT, leader TEXT, coach TEXT DEFAULT '', phone TEXT DEFAULT '')''')
        c.execute('''CREATE TABLE IF NOT EXISTS cfg_events (id INTEGER PRIMARY KEY, name TEXT, type TEXT, gender TEXT, score_rule TEXT, record TEXT, record_bonus TEXT, is_double_score BOOLEAN, need_lane BOOLEAN, has_prelim BOOLEAN, is_relay BOOLEAN, limit_count INTEGER, allowed_groups TEXT DEFAULT '')''')
        c.execute('''CREATE TABLE IF NOT EXISTS sys_config (key TEXT PRIMARY KEY, value TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS team_auth (id INTEGER PRIMARY KEY AUTOINCREMENT, team_name TEXT, password TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS registrations (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, group_name TEXT, team_id INTEGER, team_name TEXT, name TEXT, gender TEXT, bib TEXT, event_name TEXT, score TEXT DEFAULT '', rank TEXT DEFAULT '', lane TEXT DEFAULT '', heat TEXT DEFAULT '', submit_time TEXT)''')
        conn.commit()
    finally:
        conn.close()

def upgrade_records():
    conn = get_db_connection()
    try:
        c = conn.cursor()
        columns_to_add = [
            ("record", "TEXT"), ("record_bonus", "INTEGER DEFAULT 0"),
            ("duration", "REAL DEFAULT 5"), ("venue_count", "INTEGER DEFAULT 1"),
            ("allowed_groups", "TEXT DEFAULT ''"), ("is_fun", "TEXT DEFAULT '0'")
        ]
        for col, dtype in columns_to_add:
            try: c.execute(f"ALTER TABLE cfg_events ADD COLUMN {col} {dtype}")
            except Exception: pass
        conn.commit()
    finally:
        conn.close()

def force_sync_and_upgrade_db():
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS cfg_group_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name TEXT,
            event_name TEXT,
            gender TEXT,
            records_json TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS start_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            group_name TEXT, 
            event_name TEXT, 
            gender TEXT, 
            heat TEXT, 
            lane TEXT, 
            bib TEXT, 
            name TEXT, 
            team_name TEXT, 
            type TEXT, 
            total_lanes INTEGER DEFAULT 8, 
            est_time TEXT DEFAULT '', 
            time_index INTEGER DEFAULT 0, 
            is_field INTEGER DEFAULT 0,
            score TEXT DEFAULT '',
            checked_in INTEGER DEFAULT 0
        )''')
        
        for col, t in [
            ("total_lanes", "INTEGER DEFAULT 8"),
            ("est_time", "TEXT DEFAULT ''"),
            ("time_index", "INTEGER DEFAULT 0"),
            ("is_field", "INTEGER DEFAULT 0"),
            ("score", "TEXT DEFAULT ''"),
            ("checked_in", "INTEGER DEFAULT 0"),
            ("is_started", "INTEGER DEFAULT 0")
        ]:
            try: c.execute(f"ALTER TABLE start_list ADD COLUMN {col} {t}")
            except Exception: pass

        try: c.execute("ALTER TABLE registrations ADD COLUMN attempts_json TEXT DEFAULT ''")
        except Exception: pass
        try: c.execute("ALTER TABLE start_list ADD COLUMN attempts_json TEXT DEFAULT ''")
        except Exception: pass   
        try: c.execute("ALTER TABLE cfg_events ADD COLUMN qualify_count INTEGER DEFAULT 8")
        except Exception: pass
        try: c.execute("ALTER TABLE registrations ADD COLUMN relay_leg TEXT DEFAULT ''")
        except Exception: pass
        try: c.execute("ALTER TABLE cfg_teams ADD COLUMN coach TEXT DEFAULT ''")
        except Exception: pass
        try: c.execute("ALTER TABLE cfg_teams ADD COLUMN phone TEXT DEFAULT ''")
        except Exception: pass
        try: c.execute("ALTER TABLE cfg_events ADD COLUMN is_team TEXT DEFAULT '0'")
        except Exception: pass
        try: c.execute("ALTER TABLE cfg_events ADD COLUMN dense_rank TEXT DEFAULT '0'")
        except Exception: pass
        try: c.execute("ALTER TABLE cfg_teams ADD COLUMN custom_scores TEXT DEFAULT '{}'")
        except Exception: pass
        try: c.execute("ALTER TABLE start_list ADD COLUMN is_manual_final INTEGER DEFAULT 0")
        except Exception: pass
        try: c.execute("ALTER TABLE start_list ADD COLUMN points INTEGER DEFAULT 0")
        except Exception: pass
        try: c.execute("ALTER TABLE start_list ADD COLUMN rank TEXT DEFAULT ''")
        except Exception: pass
        try: c.execute("ALTER TABLE start_list ADD COLUMN record_bonus INTEGER DEFAULT 0")
        except Exception: pass
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_reg_lookup ON registrations (group_name, gender, event_name);")
            c.execute("CREATE INDEX IF NOT EXISTS idx_reg_team ON registrations (team_id, name);")
            c.execute("CREATE INDEX IF NOT EXISTS idx_start_lookup ON start_list (group_name, gender, event_name, heat);")
            c.execute("CREATE INDEX IF NOT EXISTS idx_start_schedule ON start_list (time_index, heat, lane);")
            c.execute("CREATE INDEX IF NOT EXISTS idx_start_event_single ON start_list (event_name);")
            c.execute("CREATE INDEX IF NOT EXISTS idx_reg_event_single ON registrations (event_name);")
        except Exception as e:
            print("索引创建跳过:", e)

        conn.commit()
    finally:
        conn.close()

def init_template_table():
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS sys_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            description TEXT DEFAULT '',
            data_json TEXT,
            created_at TEXT
        )''')
        conn.commit()
    finally:
        conn.close()

@app.route('/api/update_team_custom_score', methods=['POST'])
@login_required('admin')
def update_team_custom_score():
    data = request.json or {}
    team_name = str(data.get('team_name') or '').strip()
    group_name = str(data.get('group_name') or '').strip()
    item_key = str(data.get('item_key') or '').strip()
    val = float(data.get('score', 0) or 0)

    conn = get_db_connection()
    c = conn.cursor()
    try:
        row = c.execute("SELECT custom_scores FROM cfg_teams WHERE TRIM(name) = ? OR REPLACE(name, ' ', '') = REPLACE(?, ' ', '')", (team_name, team_name)).fetchone()
        scores = {}
        if row and row['custom_scores']:
            try: scores = json.loads(row['custom_scores'])
            except Exception: scores = {}

        scores[item_key] = val

        c.execute("UPDATE cfg_teams SET custom_scores = ? WHERE TRIM(name) = ? OR REPLACE(name, ' ', '') = REPLACE(?, ' ', '')", 
                  (json.dumps(scores, ensure_ascii=False), team_name, team_name))
        conn.commit()

        # 🌟 必须彻底清理服务端该组别的所有榜单缓存！
        if '_cache_store' in globals():
            for k in list(_cache_store.keys()):
                if k.startswith("rank_") or k.startswith("team_detail_"):
                    _cache_store.pop(k, None)

        return jsonify({"status": "success", "msg": "已更新", "score": val, "team_name": team_name, "item_key": item_key})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
# 2. 增加/删除系统附加项目标签列（如新增一个“入场式”列）
@app.route('/api/manage_custom_score_columns', methods=['POST'])
@login_required('admin')
def manage_custom_score_columns():
    data = request.json or {}
    columns = data.get('columns', [])
    conn = get_db_connection()
    try:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO sys_config (key, value) VALUES ('custom_score_cols', ?)", (json.dumps(columns, ensure_ascii=False),))
        conn.commit()
        return jsonify({"status": "success", "columns": columns})
    finally:
        conn.close()
@app.route('/api/get_group_records', methods=['POST'])
def get_group_records():
    data = request.json or {}
    g_name = data.get('group_name')

    cache_key = f"group_records_{g_name}"
    cached = get_cached_data(cache_key, ttl_seconds=300) # 缓存 5 分钟
    if cached is not None:
        return jsonify(cached)

    conn = get_db_connection()
    c = conn.cursor()
    try:
        rows = c.execute("SELECT event_name, gender, records_json FROM cfg_group_records WHERE group_name=?", (g_name,)).fetchall()
        res = {}
        for r in rows:
            res[f"{r['event_name']}_{r['gender']}"] = json.loads(r['records_json'])
        set_cached_data(cache_key, res, ttl_seconds=300)
        return jsonify(res)
    finally:
        conn.close()
@app.route('/api/save_group_records', methods=['POST'])
@login_required('admin')
def save_group_records():
    data = request.json or {}
    g_name = data.get('group_name')
    records_dict = data.get('records', {})
    
    if not g_name:
        return jsonify({"status": "error", "msg": "缺少组别名称"}), 400

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        # 1. 清空当前组别的旧记录
        c.execute("DELETE FROM cfg_group_records WHERE group_name = ?", (g_name,))
        
        # 2. 严谨解析 key (从右侧最后一个下划线切分性别，防止项目名带有下划线)
        for key, rec_list in records_dict.items():
            if '_' in key:
                parts = key.rsplit('_', 1)
                e_name = parts[0].strip()
                gender = parts[1].strip()
            else:
                e_name = key.strip()
                gender = ''
                
            json_str = json.dumps(rec_list, ensure_ascii=False)
            c.execute("""
                INSERT INTO cfg_group_records (group_name, event_name, gender, records_json) 
                VALUES (?, ?, ?, ?)
            """, (g_name, e_name, gender, json_str))
            
        conn.commit()
        
        # 🌟 关键：清除该组别对应的记录缓存，确保前端能立即读出新存的数据
        if '_cache_store' in globals():
            _cache_store.pop(f"group_records_{g_name}", None)

        return jsonify({"status": "success", "msg": "纪录保存成功！"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": "保存失败: " + str(e)}), 500
    finally:
        conn.close()
@app.route('/api/analyze_physical_data', methods=['POST'])
def analyze_physical_data():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        # 🌟 修复：多表关联，如果 registrations 里的 group_name 缺失，自动通过 cfg_teams 补齐
        sql = """
            SELECT 
                COALESCE(NULLIF(r.group_name, ''), g.name, '未分配组别') AS group_name,
                r.team_name, r.gender, r.event_name, r.score
            FROM registrations r
            LEFT JOIN cfg_teams t ON (r.team_id = t.id OR r.team_name = t.name)
            LEFT JOIN cfg_groups g ON (t.group_id = g.id)
            WHERE r.score IS NOT NULL AND TRIM(r.score) != ''
              AND r.score NOT LIKE '%弃权%'
              AND r.score NOT LIKE '%DNS%'
              AND r.score NOT LIKE '%DQ%'
              AND r.score NOT LIKE '%DNF%'
        """
        rows = c.execute(sql).fetchall()        
        cfgs = {r['name']: dict(r) for r in c.execute("SELECT name, type FROM cfg_events").fetchall()}
        grade_analysis = {}

        for r in rows:
            g_name = r['group_name'] or '未分配组别'
            t_name = r['team_name'] or '未知班级'
            gen = '女' if '女' in str(r['gender']) else '男'
            raw_evt = r['event_name']
            
            core_evt = re.sub(r"\(.*?\)|（.*?）|决赛|预赛|男子|女子|混合|第\d+组|场地\d+", "", raw_evt).strip()
            if not core_evt:
                continue

            sec_val = parse_time_to_seconds(r['score'])
            if sec_val <= 0:
                continue

            # 判定项目类别
            evt_category = 'timing'
            if any(k in core_evt for k in ['仰卧', '起坐', '跳绳', '垫球', '引体', '踢毽', '计数', '个数', '投篮']):
                evt_category = 'count'
            elif any(k in core_evt for k in ['立定跳远', '跳远', '跳高', '实心球', '铅球', '标枪', '铁饼', '投', '掷']):
                evt_category = 'distance'
            else:
                cfg = cfgs.get(core_evt)
                if cfg and (cfg.get('type') == '田赛' or '田' in str(cfg.get('type'))):
                    evt_category = 'distance'
                else:
                    evt_category = 'timing'

            if g_name not in grade_analysis:
                grade_analysis[g_name] = {"events": {}, "teams": {}}

            if core_evt not in grade_analysis[g_name]["events"]:
                grade_analysis[g_name]["events"][core_evt] = {
                    "男": [],
                    "女": [],
                    "category": evt_category
                }
            if gen in grade_analysis[g_name]["events"][core_evt]:
                grade_analysis[g_name]["events"][core_evt][gen].append(sec_val)

            if t_name not in grade_analysis[g_name]["teams"]:
                grade_analysis[g_name]["teams"][t_name] = {}
            if core_evt not in grade_analysis[g_name]["teams"][t_name]:
                grade_analysis[g_name]["teams"][t_name][core_evt] = {"男": [], "女": []}
            if gen in grade_analysis[g_name]["teams"][t_name][core_evt]:
                grade_analysis[g_name]["teams"][t_name][core_evt][gen].append(sec_val)

        output = {}
        for g_name, data in grade_analysis.items():
            event_summary = {}
            for evt_name, evt_data in data["events"].items():
                m_list = evt_data["男"]
                f_list = evt_data["女"]
                cat = evt_data.get("category", "timing")
                is_max_better = (cat in ['distance', 'count'])

                event_summary[evt_name] = {
                    "category": cat,
                    "is_field": (cat == 'distance'),
                    "male_avg": round(sum(m_list) / len(m_list), 2) if m_list else 0,
                    "male_count": len(m_list),
                    "male_best": (max(m_list) if is_max_better else min(m_list)) if m_list else 0,
                    "female_avg": round(sum(f_list) / len(f_list), 2) if f_list else 0,
                    "female_count": len(f_list),
                    "female_best": (max(f_list) if is_max_better else min(f_list)) if f_list else 0,
                }

            team_summary = []
            for t_name, t_evts in data["teams"].items():
                scores = {}
                for e_name, g_scores in t_evts.items():
                    all_scores = g_scores["男"] + g_scores["女"]
                    if all_scores:
                        scores[e_name] = round(sum(all_scores) / len(all_scores), 2)
                team_summary.append({"team": t_name, "scores": scores})

            output[g_name] = {
                "events": event_summary,
                "teams": team_summary
            }

        return jsonify({"status": "success", "data": output})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()

@app.route('/api/export_handbook_word')
@login_required('admin')
def export_handbook_word():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        cfg_rows = c.execute("SELECT key, value FROM sys_config").fetchall()
        config = {r['key']: r['value'] for r in cfg_rows}
        title = config.get('title', '田径运动会')

        groups = [dict(r) for r in c.execute("SELECT * FROM cfg_groups ORDER BY IFNULL(sort_order, id) ASC").fetchall()]
        teams = [dict(r) for r in c.execute("SELECT * FROM cfg_teams ORDER BY group_id ASC, IFNULL(sort_order, id) ASC").fetchall()]
        events = [dict(r) for r in c.execute("SELECT * FROM cfg_events ORDER BY id ASC").fetchall()]

        # 读取最新保存的开赛日期
        c_date = c.execute("SELECT value FROM system_settings WHERE key='start_date'").fetchone()
        base_start_date = c_date[0] if (c_date and c_date[0]) else ""

        qualify_map = {}
        for e in events:
            q_cnt = e.get('qualify_count') or 8
            has_prelim = (str(e.get('has_prelim')) == '1' or str(e.get('hasPrelim')).lower() == 'true')
            qualify_map[e['name']] = f"按成绩录取{q_cnt}名决赛" if has_prelim else f"录取{q_cnt}名"

        regs = [dict(r) for r in c.execute("""
            SELECT group_name, team_name, name, gender, bib, event_name 
            FROM registrations 
            WHERE name != '' AND event_name NOT LIKE '%决赛%'
            ORDER BY group_name ASC, team_name ASC, gender ASC, bib ASC, name ASC
        """).fetchall()]

        # 严格按照 start_list 数据库中的真实物理存储排期读取
        schedule = [dict(r) for r in c.execute("""
            SELECT id, group_name, event_name, gender, heat, lane, bib, name, team_name, type, est_time, time_index, is_field
            FROM start_list
            ORDER BY 
                CAST(IFNULL(time_index, 0) AS INTEGER) ASC, 
                group_name ASC,
                gender ASC,
                event_name ASC,
                CAST(IFNULL(heat, 1) AS INTEGER) ASC, 
                CAST(IFNULL(lane, 1) AS INTEGER) ASC,
                id ASC
        """).fetchall()]

        active_time_slots = []
        for s in schedule:
            if not s.get('name'): continue
            est_raw = s.get('est_time', '')
            m = re.search(r"第(\d+)天(上午|下午)?", est_raw)
            if m:
                d_num = int(m.group(1))
                ampm = m.group(2) or "上午"
                slot_key = (d_num, 0 if ampm == "上午" else 1, ampm)
                if slot_key not in active_time_slots:
                    active_time_slots.append(slot_key)

        active_time_slots.sort(key=lambda x: (x[0], x[1]))

        cn_numbers = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}
        cn_to_num = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

        def get_unit_sort_weight(title_str):
            m = re.search(r"第([一二三四五六七八九十\d]+)单元", title_str)
            if m:
                val = m.group(1)
                return cn_to_num.get(val, int(val) if val.isdigit() else 99)
            return 99
        slot_unit_title_map = {}
        for unit_idx, (d_num, _, ampm) in enumerate(active_time_slots, start=1):
            date_val = ""
            if base_start_date:
                try:
                    base_d = datetime.strptime(base_start_date, "%Y-%m-%d")
                    cur_d = base_d + timedelta(days=d_num - 1)
                    date_val = cur_d.strftime("%Y年%m月%d日")
                except Exception:
                    pass
            if not date_val:
                date_val = datetime.now().strftime("%Y年%m月%d日")

            u_cn = cn_numbers.get(unit_idx, str(unit_idx))
            slot_unit_title_map[(d_num, ampm)] = f"第{u_cn}单元 ({date_val}  {ampm})"

        def parse_unit_date(est_time_str):
            if not est_time_str:
                return "第一单元 (比赛日)", "08:30"
            m = re.search(r"第(\d+)天(上午|下午)?\s*(\d+[:：]\d+)?", est_time_str)
            if m:
                d_num = int(m.group(1))
                ampm = m.group(2) or "上午"
                t_str = (m.group(3) or "09:00").replace('：', ':')
                u_title = slot_unit_title_map.get((d_num, ampm), f"第{d_num}天 ({ampm})")
                return u_title, t_str
            return "第一单元 (比赛日)", "09:00"

        # 1. 参赛单位名单构建
        team_athletes_map = {}
        for t in teams:
            team_athletes_map[t['name']] = {'leader': t.get('leader', ''), 'coach': t.get('coach', ''), 'groups': {}}

        seen_ath = set()
        for r in regs:
            ath_key = (r['team_name'], r['name'], r['group_name'], r['gender'])
            if ath_key in seen_ath: continue
            seen_ath.add(ath_key)

            t_n = r['team_name']
            if t_n not in team_athletes_map:
                team_athletes_map[t_n] = {'leader': '', 'coach': '', 'groups': {}}

            prefix = f"{'男' if r['gender'] == '男' else '女'}子"
            g_clean = r['group_name'].replace('男子', '').replace('女子', '')
            g_label = f"{prefix}{g_clean}"

            if g_label not in team_athletes_map[t_n]['groups']:
                team_athletes_map[t_n]['groups'][g_label] = []

            raw_bib = str(r['bib'] or '').strip()
            bib_str = raw_bib.zfill(4) if raw_bib.isdigit() else raw_bib
            team_athletes_map[t_n]['groups'][g_label].append({'bib': bib_str, 'name': r['name']})

        def get_team_grade_weight(team_name):
            tn = team_name.strip()
            # 年级权重：初一(10) -> 初二(20) -> 初三(30) -> 高一(40) -> 高二(50) -> 高三(60)
            if re.search(r'初一|七年级|初1|七1|7年级', tn): return 10
            if re.search(r'初二|八年级|初2|八2|8年级', tn): return 20
            if re.search(r'初三|九年级|初3|九3|9年级', tn): return 30
            if re.search(r'高一|高1|10年级', tn): return 40
            if re.search(r'高二|高2|11年级', tn): return 50
            if re.search(r'高三|高3|12年级', tn): return 60
            return 99

        # 主排序：年级由低到高；次排序：班级数字自然递增（1班、2班...）
        sorted_team_names = sorted(
            [t_name for t_name, t_info in team_athletes_map.items() if t_info['groups']],
            key=lambda name: (get_team_grade_weight(name), [int(text) if text.isdigit() else text for text in re.split(r'(\d+)', name)])
        )

        teams_html = "<h1 style='text-align:center; font-size:26pt; font-weight:bold; letter-spacing:4px; margin-bottom:10pt;'>代表队名单</h1><div style='text-align:center; font-size:16pt; font-weight:bold; margin-bottom:25pt;'>(参赛单位)</div>"
        for t_name in sorted_team_names:
            t_info = team_athletes_map[t_name]
            leader_txt = t_info['leader'] if t_info['leader'] else "—"
            coach_txt = t_info['coach'] if t_info.get('coach') else leader_txt
            teams_html += f"<div style='margin-bottom:28pt; page-break-inside:avoid;'><div style='text-align:center; font-size:18pt; font-weight:bold; margin-bottom:12pt;'>{t_name}</div><div style='font-size:11pt; line-height:1.6;'>领队：{leader_txt}</div><div style='font-size:11pt; line-height:1.6; margin-bottom:8pt;'>教练：{coach_txt}</div>"
            for g_label, ath_list in t_info['groups'].items():
                teams_html += f"<div style='font-size:12pt; font-weight:bold; margin-top:8pt; margin-bottom:4pt;'>{g_label}</div><table style='width:100%; border-collapse:collapse; font-size:10.5pt; table-layout:fixed; border:none; margin-bottom:8pt;'>"
                col_count = 6
                header_tds = "".join([f"<td style='border:none; width:8%; font-weight:bold;'>编号</td><td style='border:none; width:8.6%; font-weight:bold;'>姓名</td>" for _ in range(col_count)])
                teams_html += f"<tr style='height:22px;'>{header_tds}</tr>"
                for i in range(0, len(ath_list), col_count):
                    chunk = ath_list[i:i+col_count]
                    teams_html += "<tr style='height:22px;'>"
                    for item in chunk:
                        teams_html += f"<td style='border:none; font-family:\"Times New Roman\";'>{item['bib']}</td><td style='border:none;'>{item['name']}</td>"
                    for _ in range(col_count - len(chunk)):
                        teams_html += "<td style='border:none;'></td><td style='border:none;'></td>"
                    teams_html += "</tr>"
                teams_html += "</table>"
            teams_html += "</div>"

        # 2. 项目分类规则
        event_setting_rules = []
        for e in sorted(events, key=lambda x: len(x.get('name', '')), reverse=True):
            raw_t = (e.get('type') or '').strip()
            if str(e.get('is_fun')) == '1' or raw_t == '趣味': raw_t = '趣味项目'
            name_clean = (e.get('name') or '').strip()
            if name_clean:
                event_setting_rules.append((name_clean, raw_t))

        def get_exact_category(event_full_name, is_field_flag):
            efn = event_full_name.strip()
            for s_name, s_type in event_setting_rules:
                if s_name in efn:
                    if s_type == '健康测试' or '健康' in s_type: return '健康测试'
                    elif s_type in ['趣味', '趣味项目']: return '趣味项目'
                    elif s_type == '田赛': return '田  赛'
                    elif s_type == '径赛': return '径  赛'

            if any(k in efn for k in ['仰卧', '引体', '体前屈', '肺活量', '视力', '立定跳远', '健康', '体测']):
                return '健康测试'
            elif any(k in efn for k in ['篮球', '绕杆', '排球', '趣味', '拔河', '跳绳', '投篮', '穿梭', '袋鼠', '毛毛虫']):
                return '趣味项目'
            elif any(k in efn for k in ['跳高', '跳远', '三级跳', '铅球', '实心球', '标枪', '铁饼']):
                return '田  赛'
            return '田  赛' if is_field_flag == 1 else '径  赛'

        # 3. 构建竞赛日程表数据映射
        schedule_unit_map = {}
        # 🌟 修复关键：补齐 heats_task_map 的初始化与聚类逻辑！
        heats_task_map = {}

        for s in schedule:
            if not s['name']: continue
            u_title, time_val = parse_unit_date(s.get('est_time', ''))
            category = get_exact_category(s['event_name'], s.get('is_field', 0))

            # A. 日程表映射
            task_key = (u_title, category, s['group_name'], s['event_name'], s['gender'])
            if task_key not in schedule_unit_map:
                schedule_unit_map[task_key] = {
                    'time': time_val,
                    'time_index': s.get('time_index') or 0,
                    'heats': set(),
                    'count': 0
                }
            schedule_unit_map[task_key]['heats'].add(s['heat'])
            schedule_unit_map[task_key]['count'] += 1

            # B. 分组名单映射 (heats_task_map)
            s_name = str(s.get('name') or '').strip()
            s_team = str(s.get('team_name') or '').strip()
            if '待定' in s_name or '预赛出线' in s_team:
                continue

            ek = (u_title, category, s['group_name'], s['event_name'], s['gender'])
            if ek not in heats_task_map:
                heats_task_map[ek] = {}
            h_no = str(s['heat'])
            if h_no not in heats_task_map[ek]:
                heats_task_map[ek][h_no] = []
            heats_task_map[ek][h_no].append(s)

        unit_schedule_tree = {}
        for (u_title, cat, g_name, e_name, gen), info in schedule_unit_map.items():
            if u_title not in unit_schedule_tree:
                unit_schedule_tree[u_title] = {'径  赛': [], '田  赛': [], '趣味项目': [], '健康测试': []}
            if cat not in unit_schedule_tree[u_title]:
                unit_schedule_tree[u_title][cat] = []

            clean_core = re.sub(r'[\(（].*?[\)）]', '', e_name).replace('预赛', '').replace('决赛', '').strip()
            for prefix_k in ['男子', '女子', '男', '女', g_name]:
                if clean_core.startswith(prefix_k):
                    clean_core = clean_core[len(prefix_k):].strip()

            round_str = "预赛" if '预赛' in e_name else "决赛"
            prefix = f"{'男' if gen == '男' else '女'}子"
            g_clean = g_name.replace('男子', '').replace('女子', '')
            full_item_name = f"{prefix}{g_clean}{clean_core}"

            q_principle = qualify_map.get(clean_core) or ("按成绩录取8名决赛" if round_str == "预赛" else "录取8名")

            unit_schedule_tree[u_title][cat].append({
                'time': info['time'],
                'time_index': info['time_index'],
                'event_full_name': full_item_name,
                'round': round_str,
                'count_str': f"{info['count']}人" if ('接力' not in e_name and '4x' not in e_name.lower()) else f"{info['count']}队",
                'heats_str': f"{len(info['heats'])}组",
                'qualify': q_principle
            })

        schedule_html = "<h1 style='text-align:center; font-size:24pt; font-weight:bold; letter-spacing:8px; margin-bottom:14pt;'>竞 赛 日 程</h1>"

        for u_title in sorted(unit_schedule_tree.keys(), key=get_unit_sort_weight):
            schedule_html += f"<div style='text-align:center; font-size:14pt; font-weight:bold; margin-top:12pt; margin-bottom:6pt;'><u>{u_title}</u></div>"
            cats = unit_schedule_tree[u_title]

            for cat_title in ['径  赛', '田  赛', '趣味项目', '健康测试']:
                items = cats.get(cat_title, [])
                if not items: continue
                items.sort(key=lambda x: (x['time_index'], x['time']))

                schedule_html += f"""
                <div style='text-align:center; font-size:13pt; font-weight:bold; margin-top:8pt; margin-bottom:4pt;'>{cat_title}</div>
                <table style='width:100%; border-collapse:collapse; text-align:center; font-size:9.5pt; margin-bottom:12pt; border-top:1.5pt solid #000; border-bottom:1.5pt solid #000; table-layout:fixed;'>
                    <tr style='height:22px; border-bottom:0.75pt solid #000; font-weight:bold;'>
                        <th style='width:36px; border:none;'>序号</th>
                        <th style='width:65px; border:none;'>比赛时间</th>
                        <th style='width:32%; border:none; text-align:left; padding-left:8px; white-space:nowrap;'>项目名称</th>
                        <th style='width:45px; border:none;'>赛次</th>
                        <th style='width:45px; border:none;'>人数</th>
                        <th style='width:45px; border:none;'>组数</th>
                        <th style='border:none; text-align:left; padding-left:6px; white-space:nowrap;'>录取原则</th>
                    </tr>
                """
                for idx, row in enumerate(items, 1):
                    schedule_html += f"""
                    <tr style='height:19px; border:none;'>
                        <td style='border:none;'>{idx}</td>
                        <td style='border:none; font-family:"Times New Roman";'>{row['time']}</td>
                        <td style='border:none; text-align:left; padding-left:8px; white-space:nowrap; overflow:hidden; font-weight:bold;'>{row['event_full_name']}</td>
                        <td style='border:none;'>{row['round']}</td>
                        <td style='border:none;'>{row['count_str']}</td>
                        <td style='border:none;'>{row['heats_str']}</td>
                        <td style='border:none; text-align:left; padding-left:6px; white-space:nowrap;'>{row['qualify']}</td>
                    </tr>
                    """
                schedule_html += "</table>"

        # 4. 构建竞赛分组名单（紧凑排版）
        groups_html = "<h1 style='text-align:center; font-size:24pt; font-weight:bold; letter-spacing:4px; margin-bottom:14pt;'>竞赛分组名单</h1>"
        units_ordered = sorted(list(set([k[0] for k in heats_task_map.keys()])), key=get_unit_sort_weight)
        for u_title in units_ordered:
            groups_html += f"<div style='text-align:center; font-size:14pt; font-weight:bold; margin-top:14pt; margin-bottom:10pt;'><u>{u_title}</u></div>"
            for cat_type in ['径  赛', '田  赛', '趣味项目', '健康测试']:
                target_keys = [k for k in heats_task_map.keys() if k[0] == u_title and k[1] == cat_type]
                if not target_keys: continue

                groups_html += f"<div style='font-size:14pt; font-weight:bold; margin-top:10pt; margin-bottom:6pt; border-bottom:1pt solid #000; padding-bottom:2pt;'>{cat_type}</div>"
                item_idx = 1

                for (u_t, cat, g_name, e_name, gen) in target_keys:
                    clean_core = re.sub(r'[\(（].*?[\)）]', '', e_name).replace('预赛', '').replace('决赛', '').strip()
                    for prefix_k in ['男子', '女子', '男', '女', g_name]:
                        if clean_core.startswith(prefix_k):
                            clean_core = clean_core[len(prefix_k):].strip()

                    round_str = "预赛" if '预赛' in e_name else "决赛"
                    prefix = f"{'男' if gen == '男' else '女'}子"
                    g_clean = g_name.replace('男子', '').replace('女子', '')
                    full_event_title = f"{prefix}{g_clean}{clean_core}{round_str}"

                    heat_dict = heats_task_map[(u_t, cat, g_name, e_name, gen)]
                    total_p = sum(len(lst) for lst in heat_dict.values())
                    total_h = len(heat_dict)
                    if total_p == 0:
                        continue
                    unit_label = "人" if ('接力' not in e_name and '4x' not in e_name.lower()) else "队"

                    # 区分径赛（单组单排）与田赛/趣味项目（人数多分多排）
                    is_track_event = ('径' in cat_type)

                    if is_track_event:
                        groups_html += f"<div style='font-size:11.5pt; font-weight:bold; margin-top:8pt; margin-bottom:2pt; page-break-after:avoid;'>{item_idx} . {full_event_title}  共{total_p}{unit_label}  共{total_h}组</div>"
                        item_idx += 1

                        for h_no in sorted(heat_dict.keys(), key=lambda x: int(x)):
                            h_athletes = heat_dict[h_no]
                            h_athletes.sort(key=lambda x: int(x['lane']) if str(x.get('lane', '')).isdigit() else 99)

                            t_display = "09:00"
                            m = re.search(r"\d+[:：]\d+", h_athletes[0].get('est_time', ''))
                            if m: t_display = m.group(0).replace('：', ':')

                            lane_map = {int(a['lane']): a for a in h_athletes if str(a.get('lane', '')).isdigit()}
                            max_lane = max(8, max(lane_map.keys()) if lane_map else 8)

                            groups_html += f"""
                            <div style='margin-bottom:8pt; page-break-inside:avoid;'>
                                <div style='font-size:10pt; font-weight:bold; margin-top:5pt; margin-bottom:2pt;'>第{h_no}组 {t_display}</div>
                                <table style='width:100%; border-collapse:collapse; font-size:9pt; table-layout:fixed; border:none;'>
                            """

                            lane_th_row = ""
                            bib_td_row = ""
                            name_td_row = ""
                            team_td_row = ""
                            is_relay = ('接力' in e_name or '4x' in e_name.lower())

                            for l_idx in range(1, max_lane + 1):
                                ath = lane_map.get(l_idx)
                                w_pct = f"{100/max_lane:.2f}%"
                                lane_th_row += f"<td style='border-top:0.75pt solid #000; border-bottom:0.75pt solid #000; border-left:none; border-right:none; width:{w_pct}; text-align:center; padding:2px 0;'>{l_idx if ath else ''}</td>"
                                if ath:
                                    raw_b = str(ath.get('bib') or '').strip()
                                    b_txt = raw_b.zfill(4) if raw_b.isdigit() else raw_b
                                    bib_td_row += f"<td style='border:none; text-align:center; font-family:\"Times New Roman\"; padding:1px 0;'>{b_txt if not is_relay else ''}</td>"
                                    name_td_row += f"<td style='border:none; text-align:center; font-weight:bold; padding:1px 0;'>{ath['name'] if not is_relay else ''}</td>"
                                    team_td_row += f"<td style='border:none; text-align:center; font-size:8pt; padding:1px 0; overflow:hidden; white-space:nowrap;'>{ath['team_name']}</td>"
                                else:
                                    bib_td_row += "<td style='border:none;'></td>"
                                    name_td_row += "<td style='border:none;'></td>"
                                    team_td_row += "<td style='border:none;'></td>"

                            groups_html += f"""
                                    <tr style='height:18px;'>{lane_th_row}</tr>
                                    {f"<tr style='height:16px;'>{bib_td_row}</tr>" if not is_relay else ""}
                                    {f"<tr style='height:16px;'>{name_td_row}</tr>" if not is_relay else ""}
                                    <tr style='height:16px;'>{team_td_row}</tr>
                                </table>
                            </div>
                            """
                    else:
                        # 田赛、趣味项目、健康测试：聚合所有组别的选手完整输出
                        h_athletes = []
                        for sub_heat_list in heat_dict.values():
                            h_athletes.extend(sub_heat_list)
                        h_athletes.sort(key=lambda x: (int(x.get('heat', 1) or 1), int(x['lane']) if str(x.get('lane', '')).isdigit() else 99))

                        t_display = "10:00"
                        if h_athletes and h_athletes[0].get('est_time'):
                            m = re.search(r"\d+[:：]\d+", h_athletes[0].get('est_time', ''))
                            if m: t_display = m.group(0).replace('：', ':')

                        groups_html += f"""
                        <div style='margin-top:10pt; margin-bottom:6pt; page-break-inside:avoid;'>
                            <div style='font-size:11.5pt; font-weight:bold; margin-bottom:4pt;'>
                                {item_idx} . {full_event_title}  共{total_p}人  共{total_h}组  <u>{t_display}</u>
                            </div>
                        """
                        item_idx += 1

                        chunk_size = 10
                        for i in range(0, len(h_athletes), chunk_size):
                            chunk = h_athletes[i:i + chunk_size]

                            th_cells = ""
                            bib_cells = ""
                            name_cells = ""
                            team_cells = ""

                            for seq_idx, ath in enumerate(chunk, start=i + 1):
                                raw_b = str(ath.get('bib') or '').strip()
                                b_txt = raw_b.zfill(4) if raw_b.isdigit() else raw_b

                                th_cells += f"<td style='border-top:0.75pt solid #000; border-bottom:0.75pt solid #000; border-left:none; border-right:none; width:10%; text-align:center; padding:2px 0;'>{seq_idx}</td>"
                                bib_cells += f"<td style='border:none; text-align:center; font-family:\"Times New Roman\"; padding:1px 0;'>{b_txt}</td>"
                                name_cells += f"<td style='border:none; text-align:center; font-weight:bold; padding:1px 0;'>{ath['name']}</td>"
                                team_cells += f"<td style='border:none; text-align:center; font-size:8pt; padding:1px 0; overflow:hidden; white-space:nowrap;'>{ath['team_name']}</td>"

                            rem_count = chunk_size - len(chunk)
                            for _ in range(rem_count):
                                th_cells += "<td style='border-top:0.75pt solid #000; border-bottom:0.75pt solid #000; border-left:none; border-right:none; width:10%; text-align:center;'></td>"
                                bib_cells += "<td style='border:none;'></td>"
                                name_cells += "<td style='border:none;'></td>"
                                team_cells += "<td style='border:none;'></td>"

                            groups_html += f"""
                            <table style='width:100%; border-collapse:collapse; font-size:9pt; table-layout:fixed; border:none; margin-bottom:6pt;'>
                                <tr style='height:18px;'>{th_cells}</tr>
                                <tr style='height:16px;'>{bib_cells}</tr>
                                <tr style='height:16px;'>{name_cells}</tr>
                                <tr style='height:16px;'>{team_cells}</tr>
                            </table>
                            """

                        groups_html += "</div>"

        full_word_html = f"""<html xmlns:o='urn:schemas-microsoft-com:office:office' xmlns:w='urn:schemas-microsoft-com:office:word' xmlns='http://www.w3.org/TR/REC-html40'>
        <head>
            <meta charset='utf-8'>
            <title>{title} 竞赛秩序册</title>
            <style>
                @page {{ size: A4 portrait; margin: 1.8cm 1.5cm; }}
                body {{ font-family: 'SimSun', 'Songti SC', serif; color: #000; line-height: 1.15; }}
                td, th {{ padding: 1px 2px; }}
            </style>
        </head>
        <body>
            {teams_html}
            <div style='page-break-before:always;'></div>
            {schedule_html}
            <div style='page-break-before:always;'></div>
            {groups_html}
        </body>
        </html>"""

        mem = BytesIO()
        mem.write(full_word_html.encode('utf-8'))
        mem.seek(0)
        return send_file(
            mem,
            mimetype='application/msword',
            as_attachment=True,
            download_name=f'{title}_竞赛秩序册_{datetime.now().strftime("%Y%m%d")}.doc'
        )
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
@app.route('/api/save_group_qualify_count', methods=['POST'])
@login_required('admin')
def save_group_qualify_count():
    """按【组别 + 项目】独立保存录取名次（全符号与全赛次兼容）"""
    data = request.json or {}
    g_name = data.get('group_name', '').strip()
    e_name = data.get('event_name', '').strip()
    q_count = int(data.get('qualify_count') or 8)

    clean_core = re.sub(r"\(.*?\)|（.*?）|预赛|决赛|男子|女子|混合", "", e_name).strip()
    norm_core = clean_core.replace('*', '×').replace('x', '×').replace('X', '×')

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS sys_config (key TEXT PRIMARY KEY, value TEXT)")
        row = c.execute("SELECT value FROM sys_config WHERE key = 'group_qualify_map'").fetchone()
        q_map = {}
        if row and row['value']:
            try: q_map = json.loads(row['value'])
            except Exception: q_map = {}

        # 🌟 多键冗余写入，保证不管从哪个入口查都能百分之百命中
        if g_name:
            q_map[f"{g_name}_{clean_core}"] = q_count
            q_map[f"{g_name}_{norm_core}"] = q_count
            q_map[f"{g_name}_{e_name}"] = q_count

        c.execute("INSERT OR REPLACE INTO sys_config (key, value) VALUES ('group_qualify_map', ?)",
                  (json.dumps(q_map, ensure_ascii=False),))
        conn.commit()
    finally:
        conn.close()

    # 立即触发后台数据库重新洗牌与算分
    msg = do_recalculate()
    if '_cache_store' in globals():
        _cache_store.clear()
    return jsonify({"status": "success", "msg": msg, "qualify_count": q_count})

@app.route('/api/batch_save_events', methods=['POST'])
@login_required('admin')
def batch_save_events():
    data = request.get_json() or {}
    events = data.get('events', [])
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        # 确保表中存在 is_team 列
        for col, dtype in [
            ("is_fun", "TEXT DEFAULT '0'"), ("allowed_groups", "TEXT DEFAULT ''"), 
            ("duration", "REAL DEFAULT 5"), ("venue_count", "INTEGER DEFAULT 1"),
            ("qualify_count", "INTEGER DEFAULT 8"), ("is_team", "TEXT DEFAULT '0'")
        ]:
            try: c.execute(f"ALTER TABLE cfg_events ADD COLUMN {col} {dtype}")
            except Exception: pass
        
        c.execute("DELETE FROM cfg_events")
        
        for i, e in enumerate(events):
            is_team_str = to_bool_str(e.get('isTeam') or e.get('is_team'))
            has_prelim_str = to_bool_str(e.get('hasPrelim') or e.get('has_prelim'))
            is_relay_str = to_bool_str(e.get('isRelay') or e.get('is_relay'))
            need_lane_str = to_bool_str(e.get('needLane') or e.get('need_lane'))
            is_fun_str = to_bool_str(e.get('isFun') or e.get('is_fun'))
            dense_rank_str = to_bool_str(e.get('denseRank') or e.get('dense_rank'))
            
            limit_val = e.get('limit') if e.get('limit') is not None else 8
            dur_val = e.get('duration') if e.get('duration') is not None else 5
            ven_val = e.get('venueCount') if e.get('venueCount') is not None else 1
            qualify_val = int(e.get('qualifyCount') or 8)
            
            c.execute('''
                INSERT INTO cfg_events 
                (id, name, type, gender, score_rule, record, record_bonus, is_double_score, 
                 need_lane, has_prelim, is_relay, is_team, limit_count, is_fun, allowed_groups, duration, venue_count, qualify_count, dense_rank)
                VALUES (?, ?, ?, ?, ?, '', ?, '0', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                str(i + 1), e.get('name', ''), e.get('type', ''), e.get('gender', '双性'),
                e.get('scoreRule', '9,7,6,5,4,3,2,1'), e.get('recordBonus', 0), 
                need_lane_str, has_prelim_str, is_relay_str, is_team_str,
                int(limit_val), is_fun_str,
                str(e.get('allowedGroups', '')),
                float(dur_val), int(ven_val), qualify_val, dense_rank_str
            ))
            
        conn.commit()
        _cache_store.clear()  # 关键：清除数据缓存
        return jsonify({"success": True, "message": "✅ 项目选用矩阵及录取名次配置已全量保存！"})
    except Exception as ex:
        conn.rollback()
        return jsonify({"success": False, "message": str(ex)})
    finally:
        conn.close()
@socketio.on('join_event_room')
def handle_join_event_room(data):
    room = data.get('room')
    if room:
        join_room(room)
# 🌟 1. 连接握手拦截：非管理员、非裁判一律拒连，阻止任何外部非法长连接
@socketio.on('connect')
def handle_socket_connect():
    role = session.get('user_role')
    if role not in ['admin', 'referee']:
        # 返回 False 代表拒绝本次 WebSocket 握手
        return False

# 🌟 2. 核心指令操作二次校验
@socketio.on('starter_command')
def handle_starter_command(data):
    role = session.get('user_role')
    if role not in ['admin', 'referee']:
        return  # 未授权静默丢弃，阻止捣乱
    cmd = data.get('cmd')
    payload = {
        'cmd': cmd,
        'timestamp': data.get('timestamp', time.time() * 1000),
        'heat_info': data.get('heat_info', {})
    }
    emit('starter_action', payload, broadcast=True)

@socketio.on('camera_times_pushed')
def handle_camera_times_pushed(data):
    role = session.get('user_role')
    if role not in ['admin', 'referee']:
        return
    emit('receive_camera_times', data, broadcast=True)
# 客户端离开特定项目房间
@socketio.on('leave_event_room')
def handle_leave_event_room(data):
    room = data.get('room')
    if room:
        leave_room(room)

# 客户端订阅团体总分榜更新
@socketio.on('join_rank_room')
def handle_join_rank_room(data):
    group_name = data.get('group_name')
    if group_name:
        join_room(f"rank_{group_name}")
# 初始化数据库
init_db()
force_sync_and_upgrade_db()
upgrade_records()

init_template_table()
# 后台热备份守护线程
def auto_db_backup_task():
    backup_dir = os.path.join(DATA_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    
    while True:
        # 延长检测周期并设置短超时，避免在白天高并发录入成绩时硬抢主库写锁
        gevent.sleep(3600)
        src = None
        dst = None
        try:
            if os.path.exists(DB_FILE):
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                bk_file = os.path.join(backup_dir, f"sports_auto_backup_{stamp}.db")
                
                src = sqlite3.connect(DB_FILE, timeout=5)
                dst = sqlite3.connect(bk_file)
                # 使用 pages 参数分片热备，每次备份 50 个 page，出让写锁给比赛现场接口
                src.backup(dst, pages=50)
                
                dst.close()
                src.close()
                
                bk_list = sorted([os.path.join(backup_dir, f) for f in os.listdir(backup_dir) if f.endswith('.db')])
                while len(bk_list) > 10:
                    old_file = bk_list.pop(0)
                    try: os.remove(old_file)
                    except Exception: pass
        except Exception as err:
            print(f"⚠️ [自动热备份跳过或异常]: {err}")
            if dst: 
                try: dst.close()
                except Exception: pass
            if src: 
                try: src.close()
                except Exception: pass

# 1. 轻量化搜索接口
@app.route('/api/search_query', methods=['GET'])
def search_query():
    q = request.args.get('q', '').strip()
    q_type = request.args.get('type', 'athlete')
    if not q or len(q) < 1:
        return jsonify([])

    cache_key = f"search_{q_type}_{q}"
    cached = get_cached_data(cache_key, ttl_seconds=60)
    if cached is not None:
        return jsonify(cached)

    conn = get_db_connection()
    c = conn.cursor()
    try:
        if q_type == 'athlete':
            sql = "SELECT DISTINCT name, team_name, gender, bib FROM registrations WHERE name LIKE ? OR bib = ? LIMIT 10"
            rows = c.execute(sql, (f"{q}%", q)).fetchall() 
        else:
            sql = "SELECT DISTINCT t.name, g.name as group_name FROM cfg_teams t LEFT JOIN cfg_groups g ON t.group_id = g.id WHERE t.name LIKE ? LIMIT 10"
            rows = c.execute(sql, (f"%{q}%",)).fetchall()
        res = [dict(r) for r in rows]
        set_cached_data(cache_key, res, ttl_seconds=60)
        return jsonify(res)
    finally:
        conn.close()
# 2. 个人战绩档案接口（解决“数据异常”弹窗）
@app.route('/api/get_athlete_profile', methods=['POST'])
def get_athlete_profile():
    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"status": "error", "msg": "缺少姓名"})

    cache_key = f"ath_profile_{name}"
    cached = get_cached_data(cache_key, ttl_seconds=30)
    if cached is not None:
        return jsonify(cached)

    conn = get_db_connection()
    c = conn.cursor()
    try:
        sql = "SELECT r.name, r.team_name, r.group_name, r.gender, r.bib, r.event_name, r.score, r.rank FROM registrations r WHERE r.name = ?"
        rows = c.execute(sql, (name,)).fetchall()
        if not rows:
            return jsonify({"status": "error", "msg": "未找到该运动员档案"})

        info = dict(rows[0])
        events = []
        for r in rows:
            events.append({
                "event": r['event_name'],
                "score": r['score'] if r['score'] and str(r['score']).strip() != '' else '未出成绩',
                "rank": r['rank'] if r['rank'] and str(r['rank']).strip() != '' else '--'
            })

        res_payload = {
            "status": "success",
            "name": info['name'],
            "team_name": info['team_name'],
            "group_name": info['group_name'],
            "gender": info['gender'],
            "bib": info['bib'],
            "events": events
        }
        set_cached_data(cache_key, res_payload, ttl_seconds=30)
        return jsonify(res_payload)
    finally:
        conn.close()
# ---------------- 运动会基础模板管理 ---------------- #
@app.route('/api/templates', methods=['GET'])
@login_required('admin')
def list_templates():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        rows = c.execute("SELECT id, name, description, created_at FROM sys_templates ORDER BY id DESC").fetchall()
        return jsonify({"status": "success", "templates": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()

@app.route('/api/templates/save', methods=['POST'])
@login_required('admin')
def save_as_template():
    """将当前的项目矩阵、组别、代表队、基础规则保存为模板（不含成绩与报名学生）"""
    data = request.json or {}
    template_name = str(data.get('name', '')).strip()
    desc = str(data.get('description', '')).strip()
    
    if not template_name:
        return jsonify({"status": "error", "msg": "请输入模板名称！"}), 400

    conn = get_db_connection()
    c = conn.cursor()
    try:
        # 🌟 1. 自动为旧数据库补齐缺失的字段，防止报 no such column
        try: c.execute("ALTER TABLE cfg_groups ADD COLUMN sort_order INTEGER DEFAULT 0")
        except Exception: pass
        try: c.execute("ALTER TABLE cfg_teams ADD COLUMN sort_order INTEGER DEFAULT 0")
        except Exception: pass
        try: c.execute("ALTER TABLE cfg_teams ADD COLUMN coach TEXT DEFAULT ''")
        except Exception: pass
        try: c.execute("ALTER TABLE cfg_teams ADD COLUMN phone TEXT DEFAULT ''")
        except Exception: pass

        # 🌟 2. 安全读取组别（降级兼容排序）
        try:
            groups = [dict(r) for r in c.execute("SELECT * FROM cfg_groups ORDER BY IFNULL(sort_order, rowid) ASC").fetchall()]
        except Exception:
            groups = [dict(r) for r in c.execute("SELECT * FROM cfg_groups ORDER BY rowid ASC").fetchall()]

        # 🌟 3. 安全读取代表队（降级兼容排序）
        try:
            teams = [dict(r) for r in c.execute("SELECT * FROM cfg_teams ORDER BY IFNULL(sort_order, rowid) ASC").fetchall()]
        except Exception:
            teams = [dict(r) for r in c.execute("SELECT * FROM cfg_teams ORDER BY rowid ASC").fetchall()]

        # 🌟 新增：彻底清除领队、教练、电话及往届班级自定义得分，不存入模板中
        for t in teams:
            t['leader'] = ''
            t['coach'] = ''
            t['phone'] = ''
            t['custom_scores'] = '{}'

        # 4. 读取项目配置、系统基础规则及纪录配置
        events = [dict(r) for r in c.execute("SELECT * FROM cfg_events").fetchall()]
        config = {r['key']: r['value'] for r in c.execute("SELECT * FROM sys_config").fetchall()}
        
        try:
            records = [dict(r) for r in c.execute("SELECT * FROM cfg_group_records").fetchall()]
        except Exception:
            records = []

        template_payload = {
            "groups": groups,
            "teams": teams,
            "events": events,
            "config": config,
            "records": records
        }

        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 5. 确保模板表存在
        c.execute('''CREATE TABLE IF NOT EXISTS sys_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            description TEXT DEFAULT '',
            data_json TEXT,
            created_at TEXT
        )''')

        # 6. 保存或覆盖更新模板
        c.execute("""
            INSERT INTO sys_templates (name, description, data_json, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                description = excluded.description,
                data_json = excluded.data_json,
                created_at = excluded.created_at
        """, (template_name, desc, json.dumps(template_payload, ensure_ascii=False), created_at))

        conn.commit()
        return jsonify({"status": "success", "msg": f"✅ 基础配置已成功保存为模板【{template_name}】！"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)}), 500
    finally:
        conn.close()
@app.route('/api/templates/apply', methods=['POST'])
@login_required('admin')
def apply_template():
    """将所选模板覆盖应用到当前系统"""
    data = request.json or {}
    tpl_id = data.get('id')
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        row = c.execute("SELECT name, data_json FROM sys_templates WHERE id = ?", (tpl_id,)).fetchone()
        if not row:
            return jsonify({"status": "error", "msg": "所选模板不存在！"}), 404

        tpl_data = json.loads(row['data_json'])
        c.execute("BEGIN IMMEDIATE")

        # 1. 清空并恢复组别
        c.execute("DELETE FROM cfg_groups")
        for idx, g in enumerate(tpl_data.get('groups', [])):
            c.execute("INSERT INTO cfg_groups (id, name, prefix, sort_order) VALUES (?, ?, ?, ?)",
                      (g['id'], g['name'], g.get('prefix', '-'), g.get('sort_order', idx)))

        # 2. 清空并恢复代表队
        c.execute("DELETE FROM cfg_teams")
        for idx, t in enumerate(tpl_data.get('teams', [])):
            c.execute("""
                INSERT INTO cfg_teams (id, group_id, name, leader, coach, phone, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (t['id'], t.get('group_id') or t.get('groupId'), t['name'], 
                  t.get('leader', ''), t.get('coach', ''), t.get('phone', ''), t.get('sort_order', idx)))

        # 3. 清空并恢复项目设置
        c.execute("DELETE FROM cfg_events")
        for e in tpl_data.get('events', []):
            c.execute("""
                INSERT INTO cfg_events (id, name, type, gender, score_rule, record, record_bonus,
                    is_double_score, need_lane, has_prelim, is_relay, is_team, limit_count,
                    allowed_groups, duration, venue_count, is_fun, qualify_count, dense_rank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                e.get('id'), e.get('name', ''), e.get('type', ''), e.get('gender', '双性'),
                e.get('score_rule', '9,7,6,5,4,3,2,1'), e.get('record', ''), e.get('record_bonus', 0),
                to_bool_str(e.get('is_double_score')), to_bool_str(e.get('need_lane')),
                to_bool_str(e.get('has_prelim')), to_bool_str(e.get('is_relay')),
                to_bool_str(e.get('is_team')), int(e.get('limit_count', 8)),
                e.get('allowed_groups', ''), float(e.get('duration', 5)), int(e.get('venue_count', 1)),
                to_bool_str(e.get('is_fun')), int(e.get('qualify_count', 8)),
                to_bool_str(e.get('dense_rank'))
            ))

        # 4. 恢复基础配置参数（如限报人次、报名限制等，保留沙盘日程为空由新赛事重新排）
        tpl_cfg = tpl_data.get('config', {})
        for k, v in tpl_cfg.items():
            if k not in ['manual_schedule_map']:  # 沙盘排期不强塞，让新运动会重新生成
                c.execute("INSERT OR REPLACE INTO sys_config (key, value) VALUES (?, ?)", (k, str(v)))

        # 5. 恢复纪录参数
        c.execute("DELETE FROM cfg_group_records")
        for gr in tpl_data.get('records', []):
            c.execute("INSERT INTO cfg_group_records (group_name, event_name, gender, records_json) VALUES (?, ?, ?, ?)",
                      (gr.get('group_name'), gr.get('event_name'), gr.get('gender'), gr.get('records_json')))

        # 6. 清理上一届运动员与赛程道次，保持干净整洁
        c.execute("DELETE FROM registrations")
        c.execute("DELETE FROM start_list")
        c.execute("DELETE FROM sys_config WHERE key = 'manual_schedule_map'")
        c.execute("DELETE FROM system_settings WHERE key = 'active_track_heat'")

        conn.commit()
        _cache_store.clear()
        return jsonify({"status": "success", "msg": f"🎉 成功载入模板【{row['name']}】！项目、组别与班级均已准备就绪。"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)}), 500
    finally:
        conn.close()
@app.route('/api/draft_field_score', methods=['POST'])
@login_required('referee')
def draft_field_score():
    """田赛极轻量暂存接口：仅持久化成绩与轮次明细，绝不重算积分，绝不广播全网"""
    data = request.json or {}
    start_id = data.get('id')
    score_str = str(data.get('score', '')).strip()
    attempts_json = data.get('attempts_json', '[]')

    if not start_id:
        return jsonify({"status": "error", "msg": "缺少ID"}), 400

    conn = get_db_connection()
    c = conn.cursor()
    try:
        # 单条轻量写入 start_list
        c.execute("""
            UPDATE start_list 
            SET score = ?, attempts_json = ? 
            WHERE id = ?
        """, (score_str, attempts_json, start_id))
        
        # 同步更新 registrations
        reg_id = data.get('reg_id')
        if reg_id:
            c.execute("""
                UPDATE registrations 
                SET score = ?, attempts_json = ? 
                WHERE id = ?
            """, (score_str, attempts_json, reg_id))
        
        conn.commit()
        st_row = c.execute("SELECT group_name, event_name FROM start_list WHERE id = ?", (start_id,)).fetchone()
        if st_row:
             invalidate_event_cache(st_row['event_name'], st_row['group_name'])
        return jsonify({"status": "success"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)}), 500
    finally:
        conn.close()
def background_silent_process_finals(group_name, event_name, gender):
    """
    后台静默协程：
    在系统空闲时异步检查预赛是否全员完赛，自动计算出线选手并推送给裁判端与查询端。
    """
    clean_core = re.sub(r"\(.*?\)|（.*?）|预赛|决赛|男子|女子|混合|男|女", "", event_name).strip()
    if '决赛' in event_name:
        return  # 决赛项目不需要晋级计算
    gevent.sleep(0.3)
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        
        # 1. 检查是否配置了预赛与录取名次
        cfg_row = c.execute("""
            SELECT type, has_prelim, qualify_count FROM cfg_events 
            WHERE name = ? OR name = ? OR name LIKE ?
            LIMIT 1
        """, (clean_core, event_name, f"%{clean_core}%")).fetchone()

        if not cfg_row or to_bool_str(cfg_row['has_prelim']) != '1':
            conn.close()
            return

        final_event_name = f"{clean_core} (决赛)"
        top_n = int(cfg_row['qualify_count'] or 8)

        # 2. 如果决赛已被人工锁定或已经开赛/录入，严禁覆写
        manual_chk = c.execute("""
            SELECT COUNT(*) FROM start_list 
            WHERE group_name = ? AND gender = ? 
              AND (event_name = ? OR event_name LIKE ? || '%决赛%') 
              AND (is_manual_final = 1 OR is_started = 1 OR (score != '' AND score != '-'))
        """, (group_name, gender, final_event_name, clean_core)).fetchone()[0]
        if manual_chk > 0:
            conn.close()
            return

        # 3. 检查该项目所有预赛组次是否全员跑完（含弃权）
        prelim_runners = c.execute("""
            SELECT s.checked_in, IFNULL(NULLIF(TRIM(s.score), ''), IFNULL(r.score, '')) as score
            FROM start_list s
            LEFT JOIN registrations r 
                ON s.name = r.name AND s.team_name = r.team_name AND s.group_name = r.group_name AND s.gender = r.gender
                AND (r.event_name = s.event_name OR r.event_name = REPLACE(s.event_name, ' (预赛)', '') OR r.event_name = ?)
            WHERE s.group_name = ? AND s.gender = ? 
              AND (s.event_name = ? OR s.event_name = ? || ' (预赛)' OR s.event_name LIKE ? || '%预赛%')
              AND s.name NOT LIKE '%待定%' AND s.team_name != '预赛出线'
        """, (clean_core, group_name, gender, clean_core, clean_core, clean_core)).fetchall()

        if not prelim_runners:
            conn.close()
            return

        def is_done(r):
            sc = str(r['score'] or '').strip()
            return int(r['checked_in'] or 0) == 2 or any(k in sc.upper() for k in ['弃权', 'DNS', 'DNF', 'DQ']) or (sc != '' and sc != '-')

        if not all(is_done(r) for r in prelim_runners):
            conn.close()
            return  # 还有其他预赛组未跑完，静默退出

        # 4. 全部预赛完赛，提取成绩并按田联赛规分配道次
        ath_rows = c.execute("""
            SELECT s.name, s.team_name, s.bib,
                   IFNULL(NULLIF(TRIM(s.score), ''), IFNULL(r.score, '')) AS score,
                   COALESCE(r.team_id, t.id, 0) AS team_id
            FROM start_list s
            LEFT JOIN registrations r 
                ON s.name = r.name AND s.team_name = r.team_name AND s.group_name = r.group_name AND s.gender = r.gender
                AND (r.event_name = s.event_name OR r.event_name = REPLACE(s.event_name, ' (预赛)', '') OR r.event_name = ?)
            LEFT JOIN cfg_teams t ON s.team_name = t.name
            WHERE s.group_name = ? AND s.gender = ? 
              AND (s.event_name = ? OR s.event_name = ? || ' (预赛)' OR s.event_name LIKE ? || '%预赛%')
              AND s.name NOT LIKE '%待定%' AND s.team_name != '预赛出线'
        """, (clean_core, group_name, gender, clean_core, clean_core, clean_core)).fetchall()

        valid_athletes = []
        for r in ath_rows:
            sc = str(r['score'] or '').strip()
            if any(k in sc.upper() for k in ['弃权', 'DNS', 'DNF', 'DQ']): continue
            v = parse_time_to_seconds(sc)
            if v > 0: valid_athletes.append(dict(r))

        if not valid_athletes:
            conn.close()
            return

        is_field = any(k in clean_core for k in ['跳', '投', '铅球', '标枪', '铁饼', '实心球'])
        valid_athletes.sort(key=lambda x: parse_time_to_seconds(x['score']), reverse=is_field)

        # 5. 第 top_n 名与第 top_n+1 名同分拦截
        if len(valid_athletes) > top_n:
            c_val = parse_time_to_seconds(valid_athletes[top_n - 1]['score'])
            n_val = parse_time_to_seconds(valid_athletes[top_n]['score'])
            if abs(c_val - n_val) < 0.0001:
                # 产生同分，广播通知管理员介入人工决策
                socketio.emit('finals_tie_warning', {
                    'group_name': group_name, 'event_name': final_event_name, 'gender': gender
                })
                conn.close()
                return

        finalists = valid_athletes[:top_n]
        if is_field:
            lane_order = list(range(1, len(finalists) + 1))
        else:
            lane_order = allocate_world_athletics_lanes(clean_core, len(finalists), top_n)

        # 6. 获取原赛程预设时间与索引
        slot = c.execute("""
            SELECT est_time, time_index, total_lanes, type FROM start_list 
            WHERE group_name = ? AND gender = ? AND (event_name = ? OR event_name LIKE ? || '%决赛%')
            ORDER BY id ASC LIMIT 1
        """, (group_name, gender, final_event_name, clean_core)).fetchone()

        base_est = slot['est_time'] if slot and slot['est_time'] else '第2天下午 14:30'
        base_tidx = slot['time_index'] if slot and slot['time_index'] else 800
        slot_type = slot['type'] if slot and slot['type'] else ('field' if is_field else 'sprint')
        slot_lanes = slot['total_lanes'] if slot and slot['total_lanes'] else 8

        g_row = c.execute("SELECT id FROM cfg_groups WHERE name = ?", (group_name,)).fetchone()
        gid = g_row['id'] if g_row else 0

        # 清除旧占位
        c.execute("DELETE FROM registrations WHERE group_name=? AND gender=? AND (event_name=? OR event_name=?||' (决赛)')", (group_name, gender, final_event_name, clean_core))
        c.execute("DELETE FROM start_list WHERE group_name=? AND gender=? AND (event_name=? OR event_name=?||' (决赛)')", (group_name, gender, final_event_name, clean_core))

        # 写入真实晋级选手
        for idx, ath in enumerate(finalists):
            assigned_lane = str(lane_order[idx])
            c.execute("""
                INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')
            """, (gid, group_name, ath.get('team_id', 0), ath.get('team_name', ''), ath.get('name', ''), gender, ath.get('bib', ''), final_event_name))
            
            c.execute("""
                INSERT INTO start_list (group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field, score, checked_in, is_started, is_manual_final)
                VALUES (?, ?, ?, '1', ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, 0, 0)
            """, (group_name, final_event_name, gender, assigned_lane, ath.get('bib', ''), ath.get('name', ''), ath.get('team_name', ''), slot_type, slot_lanes, base_est, base_tidx, 1 if is_field else 0))

        conn.commit()
        
        # 7. 清理静态缓存并向裁判端、查询端自动推送广播
        invalidate_event_cache(final_event_name, group_name)
        if '_cache_store' in globals():
            _cache_store.pop("get_data_public", None)
            _cache_store.pop("query_nav_events", None)

        # 🌟 自动通知裁判端、检录端与查询端
        socketio.emit('finals_promoted', {
            'event_name': final_event_name,
            'clean_core': clean_core,
            'group_name': group_name,
            'gender': gender
        })
        socketio.emit('score_updated', {'event_name': final_event_name, 'group_name': group_name, 'gender': gender, 'auto_finals_promoted': True})
        socketio.emit('meta_updated')
        socketio.emit('checkin_updated', {})

    except Exception as ex:
        if 'conn' in locals() and conn: conn.rollback()
        print(f"⚠️ [后台静默决赛晋级计算异常]: {ex}")
    finally:
        if 'conn' in locals() and conn: conn.close()
@app.route('/api/templates/delete', methods=['POST'])
@login_required('admin')
def delete_template():
    data = request.json or {}
    tpl_id = data.get('id')
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM sys_templates WHERE id = ?", (tpl_id,))
        conn.commit()
        return jsonify({"status": "success", "msg": "模板已删除！"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
if __name__ == '__main__':
    gevent.spawn(auto_db_backup_task)
    local_ip = get_host_ip()
    print("✅ 启动成功！")
    print(f"👉 领队端: http://{local_ip}:5000/bm")
    print(f"👉 管理端: http://{local_ip}:5000/admin/login")
    print(f"👉 裁判端: http://{local_ip}:5000/referee/login")
    
    app.jinja_env.auto_reload = True
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    socketio.run(app, host='0.0.0.0', port=5000)