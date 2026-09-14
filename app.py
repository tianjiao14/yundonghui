from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
import sqlite3
import json
from datetime import datetime, timedelta
import socket
import os
import random
import string
import re
from io import StringIO, BytesIO
import csv
from functools import wraps
from waitress import serve
import sys
import threading
import time

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

app.secret_key = 'sports_day_secret_key_2026'

# 全局内存缓存字典初始化
_cache_store = {}

def to_bool_str(val):
    if val is None:
        return '0'
    s = str(val).lower()
    return '1' if s in ['true', '1', 'yes', 'on'] else '0'

# 3. 数据库目录与文件路径配置
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_FILE = os.path.join(DATA_DIR, "sports_data.db")
ADMIN_PASSWORD = "admin888"
REFEREE_PASSWORD = "ref888"

# 4. 数据库连接基础函数
def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=40) 
    conn.execute('PRAGMA journal_mode=WAL;') 
    conn.row_factory = sqlite3.Row
    return conn

def parse_time_to_seconds(val):
    if not val or str(val).strip() == "": return 0.0 
    try:
        s = str(val).strip().replace('：', ':').replace('。', '.')
        if ':' in s:
            parts = s.split(':')
            if len(parts) == 2: return int(parts[0]) * 60 + float(parts[1])
            elif len(parts) == 3: return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
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

def get_cached_data(cache_key, ttl_seconds=5):
    if cache_key in _cache_store:
        data, expire_at = _cache_store[cache_key]
        if datetime.now().timestamp() < expire_at:
            return data
    return None

def set_cached_data(cache_key, data, ttl_seconds=5):
    expire_at = datetime.now().timestamp() + ttl_seconds
    _cache_store[cache_key] = (data, expire_at)

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

@app.route('/referee/login')
def referee_login(): 
    return redirect('/referee')

@app.route('/referee')
def referee():
    conn = get_db_connection()
    c = conn.cursor()
    groups = [dict(r) for r in c.execute("SELECT * FROM cfg_groups").fetchall()]
    teams = [dict(r) for r in c.execute("SELECT * FROM cfg_teams").fetchall()]
    events = [dict(r) for r in c.execute("SELECT * FROM cfg_events").fetchall()]
    conn.close()
    return render_template('referee.html', groups=groups, teams_json=json.dumps(teams), events_json=json.dumps(events))

@app.route('/query')
def query_page():
    return render_template('query.html')

# 统一认证 API
@app.route('/api/auth', methods=['POST'])
def api_auth():
    data = request.json or {}
    role_type = data.get('type')
    
    if role_type == 'admin':
        username = data.get('username')
        password = data.get('password')
        if username == 'admin' and password == ADMIN_PASSWORD:
            session['user_role'] = 'admin'
            return jsonify({'status': 'success', 'redirect': '/admin'})
        return jsonify({'status': 'fail', 'msg': '认证失败：登录名或密码错误'})
    elif role_type == 'referee':
        username = data.get('username')
        if username == 'referee' and data.get('password') == REFEREE_PASSWORD:
            session['user_role'] = 'referee'
            return jsonify({'status': 'success', 'redirect': '/referee'})
        return jsonify({'status': 'fail', 'msg': '认证失败：登录名或密码错误'})
    elif role_type == 'team':
        username = data.get('username')
        password = data.get('password')
        conn = get_db_connection()
        c = conn.cursor()
        auth_row = c.execute("SELECT password FROM team_auth WHERE team_name = ?", (username,)).fetchone()
        if not auth_row or str(auth_row['password']) != str(password):
            conn.close()
            return jsonify({'status': 'fail', 'msg': '认证失败：密码错误或账号不存在'})
        team_row = c.execute("SELECT id, group_id, name FROM cfg_teams WHERE name = ?", (username,)).fetchone()
        conn.close()
        if not team_row:
            return jsonify({'status': 'fail', 'msg': '认证失败：该代表队未配置'})
        session['user_role'] = 'team'
        session['team_id'] = team_row['id']      
        session['group_id'] = team_row['group_id']
        session['team_name'] = team_row['name']
        return jsonify({'status': 'success', 'redirect': '/bm'})
    return jsonify({'status': 'fail', 'msg': '认证失败：密码错误或账号不存在'})

@app.route('/api/logout')
def logout():
    role = session.get('user_role')
    session.clear()
    if role == 'admin':
        return redirect('/admin/login')
    elif role == 'referee':
        return redirect('/referee/login')
    return redirect('/bm')

# 检录与赛程监控 API
@app.route('/api/toggle_checkin', methods=['POST'])
def toggle_checkin():
    data = request.json or {}
    start_id = data.get('id')
    status = int(data.get('checked_in', 0))
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        
        start_row = c.execute("SELECT id, name, team_name, group_name, event_name, gender FROM start_list WHERE id = ?", (start_id,)).fetchone()
        if not start_row:
            conn.close()
            return jsonify({"status": "error", "msg": "未找到对应的道次记录"})

        c.execute("UPDATE start_list SET checked_in = ? WHERE id = ?", (status, start_id))
        
        clean_name = re.sub(r"\(.*?\)|（.*?）", "", start_row['event_name']).strip()
        
        # 🌟 修复此处缩进
        if status == 2:
            c.execute("""
                UPDATE registrations 
                SET score = '弃权' 
                WHERE name = ? AND team_name = ? AND group_name = ? 
                  AND (event_name = ? OR event_name = ?)
                  AND (score = '' OR score IS NULL OR score = '弃权')
            """, (start_row['name'], start_row['team_name'], start_row['group_name'], start_row['event_name'], clean_name))
        else:
            c.execute("""
                UPDATE registrations 
                SET score = '' 
                WHERE name = ? AND team_name = ? AND group_name = ? 
                  AND (event_name = ? OR event_name = ?)
                  AND score = '弃权'
            """, (start_row['name'], start_row['team_name'], start_row['group_name'], start_row['event_name'], clean_name))

        conn.commit()
        conn.close()

        try:
            check_and_auto_publish_finals(
                start_row['group_name'], 
                start_row['event_name'], 
                start_row['gender'] or ''
            )
        except Exception:
            pass

        return jsonify({"status": "success", "checked_in": status})
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/api/get_monitor_progress')
def get_monitor_progress():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        sql = """
            SELECT 
                s.id, s.group_name, s.event_name, s.gender, s.heat, s.lane, s.bib, s.name, s.team_name, s.est_time,
                IFNULL(s.is_started, 0) as is_started,
                IFNULL(s.checked_in, 0) as checked_in,
                r.score
            FROM start_list s
            LEFT JOIN registrations r ON s.name = r.name AND s.team_name = r.team_name AND (r.event_name = s.event_name OR r.event_name = REPLACE(REPLACE(s.event_name, ' (预赛)', ''), ' (决赛)', ''))
            ORDER BY s.time_index ASC, CAST(s.heat AS INTEGER) ASC, CAST(s.lane AS INTEGER) ASC
        """
        rows = c.execute(sql).fetchall()
        
        tasks_map = {}
        for r in rows:
            key = f"{r['group_name']}#{r['event_name']}#{r['gender']}#{r['heat']}"
            if key not in tasks_map:
                tasks_map[key] = {
                    "group_name": r['group_name'],
                    "event_name": r['event_name'],
                    "gender": r['gender'],
                    "heat": r['heat'],
                    "est_time": r['est_time'],
                    "is_started": r['is_started'],
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
            actual_runners = [a for a in t["athletes"] if a["checked_in"] != 2]
            scored_count = sum(1 for a in actual_runners if a["score"] and str(a["score"]).strip() != '')
            checked_count = sum(1 for a in t["athletes"] if a["checked_in"] == 1)
            
            if len(actual_runners) > 0 and scored_count >= len(actual_runners):
                status = "finished"
            elif t["is_started"] == 1 or scored_count > 0:
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
@app.route('/api/recalculate_all_points', methods=['POST'])
def recalculate_all_points():
    conn = get_db_connection()
    c = conn.cursor()
    count = 0
    try:
        try: c.execute("ALTER TABLE registrations ADD COLUMN points INTEGER DEFAULT 0")
        except Exception: pass
        try: c.execute("ALTER TABLE registrations ADD COLUMN record_bonus INTEGER DEFAULT 0")
        except Exception: pass

        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE registrations SET points = 0, record_bonus = 0")
        
        groups_genders = c.execute("SELECT DISTINCT group_name, gender FROM registrations WHERE group_name != ''").fetchall()
        all_cfgs = {row['name']: dict(row) for row in c.execute("SELECT * FROM cfg_events").fetchall()}

        for gg in groups_genders:
            g_name, gender = gg['group_name'], gg['gender']
            
            group_records_raw = []
            try: group_records_raw = c.execute("SELECT event_name, gender, records_json FROM cfg_group_records WHERE group_name = ?", (g_name,)).fetchall()
            except Exception: pass
            group_records_map = {}
            for gr in group_records_raw:
                group_records_map[f"{gr['event_name']}_{gr['gender']}"] = json.loads(gr['records_json'])

            rows = c.execute("SELECT DISTINCT event_name FROM registrations WHERE group_name = ? AND gender = ? AND score != ''", (g_name, gender)).fetchall()
            distinct_events = [r['event_name'] for r in rows]
            if not distinct_events: continue

            event_map = {}
            for evt in distinct_events:
                core = re.sub(r"\(.*?\)|（.*?）|决赛|预赛|及格赛|男子|女子|混合|男|女|第一组|第二组|第三组|第四组|第\d+组|场地\d+", "", evt).strip()
                if core not in event_map: event_map[core] = []
                event_map[core].append(evt)

            for core_name, sub_events in event_map.items():
                cfg = all_cfgs.get(core_name)
                if not cfg:
                    prefix = "女子" if gender == "女" else "男子"
                    cfg = all_cfgs.get(prefix + core_name)
                if not cfg: 
                    for k, v in all_cfgs.items():
                        if k in core_name or core_name in k: cfg = v; break
                
                has_prelim = False
                if cfg:
                    has_prelim = (to_bool_str(cfg.get('has_prelim') or cfg.get('hasPrelim')) == '1')

                is_field = False
                field_keywords = ['跳', '投', '掷', '铅球', '实心球', '标枪', '铁饼', '球', '引体', '仰卧']
                if cfg and (cfg.get('type') == '田赛' or '田' in str(cfg.get('type'))): is_field = True
                elif any(kwd in core_name for kwd in field_keywords): is_field = True

                placeholders = ','.join(['?'] * len(sub_events))
                sql = f"SELECT id, name, team_name, event_name, score FROM registrations WHERE group_name=? AND gender=? AND event_name IN ({placeholders}) AND score != ''"
                all_data_rows = c.execute(sql, [g_name, gender] + sub_events).fetchall()
                if not all_data_rows: continue
                
                best_score_map = {}
                for r in all_data_rows:
                    item = dict(r)
                    is_relay_evt = re.search(r'4[xX*×]|接力', item['event_name']) is not None
                    key = f"TEAM_{item['team_name']}" if is_relay_evt else f"ATH_{item['team_name']}_{item['name']}"
                    val = parse_time_to_seconds(item['score']) 
                    if val <= 0: continue
                    if key not in best_score_map:
                        best_score_map[key] = val
                    else:
                        old_val = best_score_map[key]
                        is_better = (val > old_val) if is_field else (val < old_val)
                        if is_better: best_score_map[key] = val

                target_events = []
                if has_prelim:
                    for sub_evt in sub_events:
                        if '决赛' in sub_evt: target_events.append(sub_evt)
                else:
                    for sub_evt in sub_events:
                        if '预赛' not in sub_evt: target_events.append(sub_evt)

                if not target_events: continue
                
                data_rows = [r for r in all_data_rows if r['event_name'] in target_events]
                if not data_rows: continue
                
                unique_entries = {} 
                for item in [dict(r) for r in data_rows]:
                    is_relay_event = re.search(r'4[xX*×]|接力', item['event_name']) is not None
                    key = f"TEAM_{item['team_name']}" if is_relay_event else f"ATH_{item['team_name']}_{item['name']}"
                    item['_val'] = parse_time_to_seconds(item['score']) 
                    if key not in unique_entries: unique_entries[key] = item
                    else:
                        old_val = unique_entries[key]['_val']
                        is_better = (item['_val'] > old_val) if is_field else (item['_val'] < old_val)
                        if is_better: unique_entries[key] = item

                is_height_event = any(kwd in core_name for kwd in ['跳高', '撑竿跳'])

                if is_height_event:
                    final_list = [item for item in unique_entries.values() if parse_time_to_seconds(item['score']) > 0]
                    for item in final_list:
                        item['_hj_tie'] = parse_high_jump_tie_breaker(item['score'], item.get('attempts_json', ''))
                    final_list.sort(key=lambda x: x['_hj_tie'], reverse=True)
                else:
                    final_list = [item for item in unique_entries.values() if item['_val'] > 0]
                    final_list.sort(key=lambda x: x['_val'], reverse=is_field)

                score_rule = cfg.get('score_rule', "9,7,6,5,4,3,2,1") if cfg else "9,7,6,5,4,3,2,1"
                rules = [int(x) for x in score_rule.replace('，',',').split(',') if x.strip().isdigit()]
                is_double = (to_bool_str(cfg.get('is_double_score')) == '1') if cfg else False

                my_records = group_records_map.get(f"{core_name}_{gender}", [])

                current_rank = 1
                for i, item in enumerate(final_list):
                    if i > 0:
                        if is_height_event:
                            if item['_hj_tie'] != final_list[i-1]['_hj_tie']:
                                current_rank = i + 1
                        else:
                            if item['_val'] != final_list[i-1]['_val']:
                                current_rank = i + 1
                    
                    p = 0
                    if current_rank <= len(rules):
                        p = rules[current_rank - 1]
                        if is_double: p *= 2
                    
                    is_relay_event = re.search(r'4[xX*×]|接力', item['event_name']) is not None
                    key = f"TEAM_{item['team_name']}" if is_relay_event else f"ATH_{item['team_name']}_{item['name']}"
                    best_val = best_score_map.get(key, item['_val'])

                    max_bonus = 0
                    for rec in my_records:
                        if rec.get('en'): 
                            rec_val = parse_time_to_seconds(rec.get('val'))
                            r_bonus = int(rec.get('bonus') or 0)
                            if rec_val is not None and rec_val > 0 and best_val > 0:
                                is_broken = (best_val > rec_val) if is_field else (best_val < rec_val)
                                if is_broken and r_bonus >= max_bonus:
                                    max_bonus = r_bonus
                    p += max_bonus

                    if p > 0:
                        c.execute("UPDATE registrations SET points = ?, record_bonus = ? WHERE id = ?", (p, max_bonus, item['id']))
                count += 1
        
        conn.commit()
        return jsonify({'status': 'success', 'msg': f'计算完毕！已处理 {count} 个决赛项目。多级破纪录已生效。'})
    except Exception as e:
        conn.rollback()
        return jsonify({'status': 'error', 'msg': str(e)})
    finally:
        conn.close()

@app.route('/api/delete_athlete', methods=['POST'])
def delete_athlete():
    if session.get('user_role') not in ['admin', 'team']:
        return jsonify({"status": "error", "msg": "未授权"}), 401
    data = request.json or {}
    name = data.get('name')
    team_id = data.get('team_id')
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM registrations WHERE name = ? AND team_id = ?", (name, team_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "msg": "删除成功"})

@app.route('/api/update_point', methods=['POST'])
def update_point():
    data = request.json or {}
    try:
        conn = get_db_connection()
        conn.execute("UPDATE registrations SET points = ? WHERE id = ?", (data['points'], data['id']))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'msg': '积分修改成功'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/api/calculate_team_ranking', methods=['POST'])
def calculate_team_ranking():
    data = request.json or {}
    g_name = data.get('group_name')
    
    cache_key = f"rank_{g_name}"
    cached = get_cached_data(cache_key, ttl_seconds=5)
    if cached is not None:
        return jsonify(cached)

    conn = get_db_connection()
    c = conn.cursor()
    sql = """
        SELECT 
            team_name as name, 
            SUM(points) as score,
            SUM(record_bonus) as total_record_bonus,
            SUM(CASE WHEN (points - record_bonus) >= 9 THEN 1 ELSE 0 END) as gold,
            SUM(CASE WHEN (points - record_bonus) = 7 THEN 1 ELSE 0 END) as silver,
            SUM(CASE WHEN (points - record_bonus) = 6 THEN 1 ELSE 0 END) as bronze
        FROM registrations
        WHERE group_name = ? AND points > 0
        GROUP BY team_name 
        ORDER BY score DESC, gold DESC, silver DESC
    """
    try:
        rows = c.execute(sql, (g_name,)).fetchall()
        result = [dict(r) for r in rows]
        set_cached_data(cache_key, result, ttl_seconds=5)
        return jsonify(result)
    except Exception:
        return jsonify([])
    finally:
        conn.close()

@app.route('/api/save_competition_date', methods=['POST'])
def save_competition_date():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        data = request.json or {}
        start_date = data.get('start_date', '')
        c.execute("""CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)""")
        c.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('start_date', ?)", (start_date,))
        conn.commit()
        return jsonify({"success": True, "message": "比赛时间配置成功！"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        conn.close()

# 起点裁判与终点裁判联动 API
@app.route('/api/push_active_heat', methods=['POST'])
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
        
        if active_row and active_row[0]:
            try:
                cur_active = json.loads(active_row[0])
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
                        "msg": f"⚠️ 跑道正忙！终点裁判正在录入【{cur_active['group_name']} {cur_active['event_name']} 第{cur_active['heat']}组】的成绩，尚余 {unscored_count} 人未录完，请稍候！"
                    })
            except Exception:
                pass

        clean_gender = '女' if '女' in gender else ('男' if '男' in gender else gender)
        push_time = datetime.now().strftime('%H:%M:%S')

        heat_payload = {
            "group_name": g_name,
            "event_name": e_name,
            "gender": clean_gender,
            "heat": heat,
            "push_time": push_time
        }

        c.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('active_track_heat', ?)", 
                  (json.dumps(heat_payload, ensure_ascii=False),))

        c.execute("""
            UPDATE start_list 
            SET is_started = 1 
            WHERE group_name = ? AND event_name = ? AND gender = ? AND heat = ?
        """, (g_name, e_name, clean_gender, heat))

        conn.commit()
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

@app.route('/api/get_competition_date', methods=['GET'])
def get_competition_date():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)")
        res = c.execute("SELECT value FROM system_settings WHERE key='start_date'").fetchone()
        return jsonify({"success": True, "start_date": res[0] if res else ""})
    except Exception:
        return jsonify({"success": False, "start_date": ""})
    finally:
        conn.close()

@app.route('/api/calculate_detailed_matrix', methods=['POST'])
def calculate_detailed_matrix():
    g_name = request.json.get('group_name')
    conn = get_db_connection()
    c = conn.cursor()
    try:
        sql = """
        SELECT team_name, event_name, gender, SUM(points) as pts
        FROM registrations
        WHERE group_name = ? AND points > 0
        GROUP BY team_name, event_name, gender
        """
        raw_data = c.execute(sql, (g_name,)).fetchall()
        
        matrix = {}
        all_core_events = set()
        
        for r in raw_data:
            t = r['team_name']
            full_evt = r['event_name']
            gender = r['gender']
            p = r['pts']
         
            core_evt = re.sub(r"\(.*?\)|（.*?）|决赛|预赛|及格赛|男子|女子|混合|男|女|第一组|第二组|第三组|第四组|第\d+组", "", full_evt).strip()
            all_core_events.add(core_evt)
            
            if t not in matrix: matrix[t] = {'team': t, 'total': 0, 'details': {}}
            if core_evt not in matrix[t]['details']: matrix[t]['details'][core_evt] = {'男': 0, '女': 0}
       
            g_key = '男' if '男' in gender else ('女' if '女' in gender else '男')
            if g_key in matrix[t]['details'][core_evt]:
                 matrix[t]['details'][core_evt][g_key] += p
            
            matrix[t]['total'] += p
            
        cols = sorted(list(all_core_events))
        rows = sorted(matrix.values(), key=lambda x: x['total'], reverse=True)
        return jsonify({'columns': cols, 'rows': rows})
    except Exception:
        return jsonify({'columns': [], 'rows': []})
    finally:
        conn.close()

@app.route('/api/get_team_score_details', methods=['POST'])
def get_team_score_details():
    data = request.json or {}
    g_name = data.get('group_name')
    t_name = data.get('team_name')

    conn = get_db_connection()
    c = conn.cursor()

    try:
        sql = """
            SELECT name, team_name, event_name, gender, score, points
            FROM registrations
            WHERE group_name = ? AND points > 0
        """
        rows = c.execute(sql, (g_name,)).fetchall()

        events_data = {}
        for r in rows:
            key = f"{r['event_name']}_{r['gender']}"
            if key not in events_data:
                events_data[key] = []
            events_data[key].append(dict(r))

        team_details = []

        for key, items in events_data.items():
            event_name = items[0]['event_name']
            gender = items[0]['gender']
            
            is_field = False
            field_keywords = ['跳', '投', '掷', '铅球', '实心球', '标枪', '铁饼', '球', '引体', '仰卧']
            if any(kwd in event_name for kwd in field_keywords):
                is_field = True

            for item in items:
                item['_val'] = parse_time_to_seconds(item['score'])

            items.sort(key=lambda x: x['_val'], reverse=is_field)

            current_rank = 1
            for i, item in enumerate(items):
                if i > 0 and item['_val'] != items[i-1]['_val']:
                    current_rank = i + 1

                if item['team_name'] == t_name:
                    team_details.append({
                        'event_name': event_name,
                        'gender': gender,
                        'name': item['name'],
                        'score': item['score'],
                        'rank': current_rank,
                        'points': item['points']
                    })

        team_details.sort(key=lambda x: (-x['points'], x['event_name']))
        return jsonify(team_details)
    except Exception:
        return jsonify([])
    finally:
        conn.close()

@app.route('/api/reset_system', methods=['POST'])
def reset_system():
    if session.get('user_role') != 'admin':
        return jsonify({"status": "error", "msg": "无权操作"}), 403
    
    mode = request.json.get('mode')
    conn = get_db_connection()
    c = conn.cursor()
    
    try:
        c.execute("BEGIN IMMEDIATE")
        
        if mode == 'all':
            c.execute("DELETE FROM registrations")
            c.execute("DELETE FROM start_list")
            c.execute("DELETE FROM team_auth")
            c.execute("DELETE FROM cfg_groups")
            c.execute("DELETE FROM cfg_teams")
            c.execute("DELETE FROM cfg_events")
            c.execute("DELETE FROM sqlite_sequence")
            msg = "系统已完成完全初始化，所有配置与数据已重置。"
        else:
            c.execute("""
                UPDATE registrations 
                SET score = '', 
                    rank = '', 
                    points = 0, 
                    record_bonus = 0,
                    relay_leg = ''
            """)
            c.execute("""
                UPDATE start_list 
                SET score = '',
                    checked_in = 0
            """)
            msg = "✅ 运动员成绩与积分已全部清空！报名名单、编排与系统设置已完整保留。"
            
        conn.commit()
        return jsonify({"status": "success", "msg": msg})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": "操作失败: " + str(e)})
    finally:
        conn.close()
        force_sync_and_upgrade_db()

@app.route('/api/export_teams')
def export_teams():
    conn = get_db_connection()
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
        return jsonify({"status": "success", "msg": f"✅ 导入成功！共处理 {success_groups} 个新组别，{success_teams} 个代表队！"})
    except Exception as e:
        if 'conn' in locals(): conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        if 'conn' in locals(): conn.close()

@app.route('/api/events')
@login_required('team')
def get_events():
    team_id = session.get('team_id')
    conn = get_db_connection()
    c = conn.cursor()

    row = c.execute("SELECT value FROM sys_config WHERE key='maxPerEvent'").fetchone()
    MAX_PER_EVENT = int(row[0]) if row else 3
    events = c.execute("SELECT name, type, gender, allowed_groups FROM cfg_events").fetchall()
    usage_rows = c.execute("SELECT event_name, COUNT(*) as count FROM registrations WHERE team_id=? GROUP BY event_name", (team_id,)).fetchall()
    usage_map = {r['event_name']: r['count'] for r in usage_rows}
    
    event_list = []
    for e in events:
        ename = e['name']
        etype = e['type']
        
        used = usage_map.get(ename, 0)
        if etype == '趣味':
            rem_text = "不限"
            is_full = False
        else:
            balance = MAX_PER_EVENT - used
            rem_text = f"余{max(0, balance)}"
            is_full = (balance <= 0)
        
        event_list.append({
            "name": ename,
            "type": etype,
            "gender": e['gender'],
            "allowed_groups": e['allowed_groups'],
            "rem": rem_text,
            "is_full": is_full
        })
    
    conn.close()
    return jsonify(event_list)

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
    conn = get_db_connection()
    c = conn.cursor()
    try:
        db_groups = [dict(r) for r in c.execute("SELECT * FROM cfg_groups").fetchall()]
        db_teams = [dict(r) for r in c.execute("SELECT * FROM cfg_teams").fetchall()]
        for t in db_teams: t['groupId'] = t['group_id']
        db_events = [dict(r) for r in c.execute("SELECT * FROM cfg_events").fetchall()]
        
        db_schedule = []
        try:
            raw_sch = c.execute('''SELECT id, group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field, checked_in, IFNULL(is_started, 0) as is_started 
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
                db_schedule.append(item)
        except Exception as err: 
            print(f"读取编排表异常: {err}")
        
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
        
        return jsonify({
            "status": "success",
            "groups": db_groups, 
            "teams": db_teams, 
            "events": db_events, 
            "athletes": list(athletes_map.values()), 
            "config": config, 
            "schedule": db_schedule
        })
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()

@app.route('/api/save_relay_legs', methods=['POST'])
def save_relay_legs():
    current_role = session.get('user_role')
    if current_role not in ['team', 'admin']:
        return jsonify({"status": "error", "msg": "登录会话已超时，请刷新页面重新登录代表队账号！"}), 401
        
    data = request.json or {}
    team_id = data.get('team_id')
    event_name = data.get('event_name', '')
    gender = data.get('gender', '')
    legs = data.get('legs', {})
    
    if current_role == 'team' and str(team_id) != str(session.get('team_id')):
        return jsonify({"status": "error", "msg": "越权操作：只能提交本班队伍的接力棒次！"}), 403        
    
    clean_core = re.sub(r'[\(（].*?[\)）]', '', event_name).replace('*', '×').replace('x', '×').strip()
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("""
            UPDATE registrations 
            SET relay_leg = '' 
            WHERE team_id = ? 
              AND (gender = ? OR ? = '' OR gender = '混合')
              AND (event_name LIKE ? OR event_name = ?)
        """, (team_id, gender, gender, f"%{clean_core}%", event_name))
        for leg_num, reg_id in legs.items():
            if reg_id:
                c.execute("UPDATE registrations SET relay_leg = ? WHERE id = ?", (str(leg_num), int(reg_id)))
                
        conn.commit()
        return jsonify({"status": "success", "msg": "接力棒次保存成功！"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
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
        if 'groups' in data:
            c.execute("DELETE FROM cfg_groups")
            for g in data['groups']:
                c.execute("INSERT OR REPLACE INTO cfg_groups (id, name, prefix) VALUES (?, ?, ?)", 
                          (str(g['id']), g['name'], g['prefix']))
                  
        if 'teams' in data:
            c.execute("DELETE FROM cfg_teams")
            for t in data['teams']:
                c.execute("""
                    INSERT OR REPLACE INTO cfg_teams (id, group_id, name, leader, coach, phone) 
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    str(t['id']), str(t.get('groupId') or t.get('group_id')), 
                    t['name'], t.get('leader', ''), t.get('coach', ''), t.get('phone', '')
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
                     is_double_score, need_lane, has_prelim, is_relay, limit_count, allowed_groups, duration, venue_count, is_fun) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''
                params = (
                    str(e.get('id', '')), e.get('name', ''), e.get('type', ''), e.get('gender', '双性'), 
                    str(rule), str(rec), str(bonus), 
                    to_bool_str(e.get('isDoubleScore') or e.get('is_double_score')), 
                    to_bool_str(e.get('needLane') or e.get('need_lane')), 
                    to_bool_str(e.get('hasPrelim') or e.get('has_prelim')), 
                    to_bool_str(e.get('isRelay') or e.get('is_relay')), 
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
        return jsonify({"status": "success", "msg": "✅ 配置及参赛单位已全量同步写入数据库！"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": "保存失败: " + str(e)})
    finally:
        conn.close()

@app.route('/api/save_team_staff', methods=['POST'])
def save_team_staff():
    if session.get('user_role') not in ['team', 'admin']:
        return jsonify({"status": "error", "msg": "未登录或登录已超时"}), 401
    
    data = request.json or {}
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

@app.route('/api/team_members/<int:team_id>')
def get_team_members(team_id):
    conn = get_db_connection()
    c = conn.cursor()
    rows = c.execute("SELECT id, name, gender, event_name, relay_leg FROM registrations WHERE team_id = ?", (team_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/batch_update_bibs', methods=['POST'])
def batch_update_bibs():
    if 'user_role' not in session:
        return jsonify({"status": "error", "msg": "会话已过期，请重新登录"}), 401
        
    data = request.json or {}
    athletes = data.get('athletes', [])
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        for a in athletes:
            c.execute("UPDATE registrations SET bib = ? WHERE team_id = ? AND name = ?", 
                      (str(a.get('bib', '')).strip(), str(a.get('teamId', '')), a.get('name', '')))
        conn.commit()
        return jsonify({"status": "success", "msg": "号码已成功固化到数据库！"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
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
            is_fun = evt_info and (evt_info['type'] == '趣味' or '趣味' in str(evt_info['type']) or str(evt_info['is_fun']) == '1')
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
        return jsonify({"status": "error", "msg": "未登录或登录已过期"}), 401
        
    data = request.json or {}
    team_id = str(session.get('team_id'))
    group_id = str(session.get('group_id'))
    athletes_list = data.get('athletes', [])
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        
        deadline_row = c.execute("SELECT value FROM sys_config WHERE key='regDeadline'").fetchone()
        if deadline_row and deadline_row[0]:
            try:
                deadline_dt = datetime.strptime(deadline_row[0], "%Y-%m-%dT%H:%M")
                if datetime.now() > deadline_dt:
                    return jsonify({"status": "error", "msg": f"报名通道已关闭！截止时间为：{deadline_row[0].replace('T', ' ')}"})
            except Exception:
                pass

        def get_cfg_val(key, default):
            row = c.execute("SELECT value FROM sys_config WHERE key=?", (key,)).fetchone()
            return int(row[0]) if row else default
            
        MAX_TOTAL = get_cfg_val('maxTotal', 20)
        MAX_PER_EVENT = get_cfg_val('maxPerEvent', 3)
        
        if len(athletes_list) > MAX_TOTAL:
            return jsonify({"status": "error", "msg": f"全队总人数（{len(athletes_list)}人）超过系统上限（{MAX_TOTAL}人）！"})

        event_counts = {}
        for ath in athletes_list:
            gender = ath.get('gender')
            for evt in ath.get('events', []):
                evt_info = c.execute("SELECT type, is_relay, gender FROM cfg_events WHERE name=?", (evt,)).fetchone()
                if evt_info and (evt_info['type'] == '趣味' or '趣味' in str(evt_info['type'])):
                    continue
                
                key = f"{evt}_{gender}"
                event_counts[key] = event_counts.get(key, 0) + 1
                
                is_relay = evt_info and (str(evt_info['is_relay']) == '1' or str(evt_info['is_relay']).lower() == 'true')
                limit = 4 if is_relay else MAX_PER_EVENT
                if event_counts[key] > limit:
                    return jsonify({"status": "error", "msg": f"项目【{evt}】本班【{gender}生】已报 {event_counts[key]} 人，超出限额 {limit} 人！"})

        g_info = c.execute("SELECT name FROM cfg_groups WHERE id=?", (group_id,)).fetchone()
        t_info = c.execute("SELECT name FROM cfg_teams WHERE id=?", (team_id,)).fetchone()
        g_name = g_info[0] if g_info else "未知组别"
        t_name = t_info[0] if t_info else "未知班级"
        submit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        c.execute("DELETE FROM registrations WHERE team_id=?", (team_id,))
        
        for ath in athletes_list:
            name = ath.get('name', '').strip()
            gender = ath.get('gender')
            bib = ath.get('bib', '').strip()
            for evt in ath.get('events', []):
                c.execute("""INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, submit_time)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (group_id, g_name, team_id, t_name, name, gender, bib, evt, submit_time))
        
        conn.commit()
        return jsonify({"status": "success", "msg": f"🎉 全班共 {len(athletes_list)} 名运动员名单已成功批量提交并锁定！"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": f"保存失败: {str(e)}"})
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

        c.execute("CREATE TABLE IF NOT EXISTS active_heat (id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT, event_name TEXT, gender TEXT, heat TEXT, push_time TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)")
        
        c.execute("DELETE FROM start_list")
        c.execute("DELETE FROM active_heat")
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
                (group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field, score, checked_in, is_started) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, 0)
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
    conn = get_db_connection()
    c = conn.cursor()
    try:
        event_name = data.get('event_name') or ''
        group_name = data.get('group_name') or ''
        gender = data.get('gender') or ''

        cfg = c.execute("SELECT type, is_fun FROM cfg_events WHERE name = ?", (event_name,)).fetchone()
        is_fun = cfg and (cfg['type'] == '趣味' or cfg['type'] == '趣味项目' or str(cfg['is_fun']) == '1')

        if is_fun:
            check_sql = "SELECT COUNT(*) FROM start_list WHERE event_name = ? AND (group_name = ? OR ? = '')"
            count_in_start = c.execute(check_sql, (event_name, group_name, group_name)).fetchone()[0]
            
            if count_in_start == 0:
                sql = """
                    SELECT 
                        r.id AS id, 
                        r.id AS reg_id, 
                        r.group_name, 
                        r.event_name, 
                        r.gender, 
                        '1' AS heat, 
                        '' AS lane, 
                        r.bib, 
                        r.name, 
                        r.team_name, 
                        'fun' AS type,
                        8 AS total_lanes, 
                        '' AS est_time, 
                        0 AS time_index, 
                        0 AS is_field,
                        0 AS checked_in, 
                        r.score AS score, 
                        r.score AS start_score,
                        r.points, 
                        r.record_bonus
                    FROM registrations r
                    WHERE r.event_name = ? 
                      AND (r.group_name = ? OR ? = '') 
                      AND (r.gender = ? OR ? = '')
                    ORDER BY r.group_name ASC, r.team_name ASC, r.name ASC
                """
                rows = c.execute(sql, (event_name, group_name, group_name, gender, gender)).fetchall()
                return jsonify([dict(r) for r in rows])

        is_finals = ('决赛' in event_name) and ('预赛' not in event_name)

        join_event_condition = "r.event_name = s.event_name" if is_finals else """
            (
                r.event_name = s.event_name 
                OR r.event_name = REPLACE(s.event_name, ' (预赛)', '')
                OR r.event_name = TRIM(SUBSTR(s.event_name, 1, INSTR(s.event_name || ' (场地', ' (场地') - 1))
            )
        """

        sql = f"""
            SELECT 
                s.id, s.group_name, s.event_name, s.gender, s.heat, s.lane, s.bib, s.name, s.team_name,
                s.time_index, s.est_time, s.is_field, s.checked_in, s.score AS start_score,
                r.id AS reg_id, r.score AS reg_score, r.points, r.record_bonus,
                IFNULL(r.attempts_json, s.attempts_json) AS attempts_json
            FROM start_list s
            LEFT JOIN registrations r 
                ON s.name = r.name 
                AND s.team_name = r.team_name 
                AND s.group_name = r.group_name
                AND {join_event_condition}
            WHERE (s.group_name = ? OR ? = '')
              AND (s.gender = ? OR ? = '')
              AND (
                  s.event_name = ? 
                  OR s.event_name LIKE ? || ' (场地%)'
                  OR s.event_name LIKE ? || '（场地%）'
              )
            ORDER BY s.group_name ASC, s.gender ASC, CAST(s.heat AS INTEGER) ASC, CAST(s.lane AS INTEGER) ASC
        """
        rows = c.execute(sql, (group_name, group_name, gender, gender, event_name, event_name, event_name)).fetchall()

        result = []
        for r in rows:
            item = dict(r)
            if is_finals:
                item['score'] = (item.get('start_score') if item.get('start_score') is not None and str(item.get('start_score')).strip() != '' 
                                 else (item.get('reg_score') or ''))
            else:
                raw_start = str(item.get('start_score') or '').strip()
                raw_reg = str(item.get('reg_score') or '').strip()
                item['score'] = raw_start if raw_start else raw_reg

            if not item.get('reg_id'):
                item['reg_id'] = item.get('id')

            result.append(item)

        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()

def check_and_auto_publish_finals(g_name, event_name, gender):
    clean_core = re.sub(r'\(.*?\)|（.*?）|预赛|决赛', '', event_name).strip()
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        cfg = c.execute("SELECT has_prelim, qualify_count, limit_count FROM cfg_events WHERE name=? OR name LIKE ?", 
                        (clean_core, f"%{clean_core}%")).fetchone()
        if not cfg or str(cfg['has_prelim']) not in ['1', 'true', 'True']:
            return

        top_n = int(cfg['qualify_count']) if cfg and 'qualify_count' in cfg.keys() and cfg['qualify_count'] else 8
        track_lanes = int(cfg['limit_count']) if cfg and 'limit_count' in cfg.keys() and cfg['limit_count'] else 8

        clean_gender = '女' if '女' in gender else ('男' if '男' in gender else gender)
        final_event_name = f"{clean_core} (决赛)"

        prelim_pattern = f"{clean_core}%预赛%"
        
        total_prelim = c.execute("""
            SELECT COUNT(*) FROM start_list 
            WHERE group_name = ?
              AND gender = ?
              AND (event_name LIKE ? OR event_name = ?)
        """, (g_name, clean_gender, prelim_pattern, clean_core)).fetchone()[0]

        if total_prelim == 0:
            return

        unscored = c.execute("""
            SELECT COUNT(*) FROM start_list s
            LEFT JOIN registrations r 
                ON s.name = r.name AND s.team_name = r.team_name AND s.group_name = r.group_name
                AND (r.event_name = s.event_name 
                     OR r.event_name = REPLACE(REPLACE(s.event_name, ' (预赛)', ''), '（预赛）', ''))
            WHERE s.group_name = ?
              AND s.gender = ?
              AND (s.event_name LIKE ? OR s.event_name = ?)
              AND (
                  (IFNULL(s.score, '') = '' OR s.score IS NULL)
                  AND (IFNULL(r.score, '') = '' OR r.score IS NULL)
              )
              AND IFNULL(s.checked_in, 0) != 2
        """, (g_name, clean_gender, prelim_pattern, clean_core)).fetchone()[0]

        if unscored > 0:
            return

        query = """
            SELECT DISTINCT r.name, r.team_name, r.bib, r.team_id, 
                   IFNULL(r.score, s.score) as score
            FROM registrations r
            LEFT JOIN start_list s 
                ON s.name = r.name AND s.team_name = r.team_name
            WHERE r.group_name = ?
              AND r.gender = ?
              AND (
                  r.event_name = ? 
                  OR r.event_name = ?
                  OR r.event_name LIKE ? || ' (预赛)%'
                  OR r.event_name LIKE ? || '（预赛）%'
                  OR (r.event_name LIKE ? || '%' AND r.event_name NOT LIKE '%决赛%')
              )
              AND r.score != '' AND r.score IS NOT NULL
        """
        raw_rows = c.execute(query, (
            g_name, clean_gender,
            clean_core, f"{clean_core} (预赛)", clean_core, clean_core, clean_core
        )).fetchall()

        valid_list = []
        seen = set()
        for r in raw_rows:
            name = r['name']
            if not name or name in seen: continue
            raw_s = str(r['score']).strip()
            if any(k in raw_s for k in ['弃权', 'DNS', 'DQ', 'DNF', '未到', '犯规']):
                continue
            
            sec = parse_time_to_seconds(raw_s)
            if sec > 0:
                seen.add(name)
                valid_list.append({
                    'name': name,
                    'team_name': r['team_name'] or '',
                    'team_id': r['team_id'] or 0,
                    'bib': r['bib'] or '',
                    'score': raw_s,
                    '_val': sec
                })

        if not valid_list:
            return

        valid_list.sort(key=lambda x: x['_val'])
        finalists = valid_list[:top_n]

        lane_presets = {
            8: [4, 5, 3, 6, 2, 7, 1, 8],
            6: [3, 4, 2, 5, 1, 6],
            4: [2, 3, 1, 4]
        }
        lane_order = lane_presets.get(track_lanes, [4, 5, 3, 6, 2, 7, 1, 8])

        dummy_meta = c.execute("""
            SELECT est_time, time_index, total_lanes, group_name 
            FROM start_list 
            WHERE group_name = ?
              AND gender = ?
              AND (event_name LIKE ? || '%决赛%' OR event_name = ?)
            ORDER BY time_index DESC LIMIT 1
        """, (g_name, clean_gender, clean_core, final_event_name)).fetchone()

        actual_g_name = dummy_meta['group_name'] if dummy_meta else g_name
        base_est = dummy_meta['est_time'] if (dummy_meta and dummy_meta['est_time']) else '第2天下午 14:30'
        base_tidx = dummy_meta['time_index'] if (dummy_meta and dummy_meta['time_index'] is not None) else 500
        total_lanes = dummy_meta['total_lanes'] if dummy_meta else 8

        g_info = c.execute("SELECT id FROM cfg_groups WHERE name=?", (actual_g_name,)).fetchone()
        gid = g_info['id'] if g_info else 0

        c.execute("""
            DELETE FROM start_list 
            WHERE group_name = ?
              AND gender = ?
              AND (event_name LIKE ? || '%决赛%' OR event_name = ?)
        """, (actual_g_name, clean_gender, clean_core, final_event_name))

        c.execute("""
            DELETE FROM registrations 
            WHERE group_name = ?
              AND gender = ?
              AND (event_name LIKE ? || '%决赛%' OR event_name = ?)
        """, (actual_g_name, clean_gender, clean_core, final_event_name))

        for idx, ath in enumerate(finalists):
            assigned_lane = str(lane_order[idx] if idx < len(lane_order) else (idx + 1))
            
            c.execute("""
                INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')
            """, (gid, actual_g_name, ath['team_id'], ath['team_name'], ath['name'], clean_gender, ath['bib'], final_event_name))
            
            c.execute("""
                INSERT INTO start_list (group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field, score, checked_in, is_started)
                VALUES (?, ?, ?, '1', ?, ?, ?, ?, 'sprint', ?, ?, ?, 0, '', 0, 0)
            """, (actual_g_name, final_event_name, clean_gender, assigned_lane, ath['bib'], ath['name'], ath['team_name'], total_lanes, base_est, base_tidx))

        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()

@app.route('/api/submit_score', methods=['POST'])
@login_required('referee')
def submit_score():
    data = request.json or {}
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        raw_val = str(data.get('score', '')).strip()
        reg_id = data.get('id') or data.get('reg_id')
        
        raw_attempts = data.get('attempts')
        if raw_attempts is None:
            attempts_json = ''
        elif isinstance(raw_attempts, str):
            attempts_json = raw_attempts
        else:
            attempts_json = json.dumps(raw_attempts, ensure_ascii=False)
        
        if not reg_id:
            conn.close()
            return jsonify({"status": "error", "msg": "缺少记录ID，请刷新页面重试"})

        row = c.execute("SELECT id, event_name, team_name, name, group_name, gender FROM registrations WHERE id=?", (reg_id,)).fetchone()
        
        target_reg_id = reg_id
        if not row:
            start_row = c.execute("SELECT id, event_name, team_name, name, group_name, gender FROM start_list WHERE id=?", (reg_id,)).fetchone()
            if start_row:
                clean_name = re.sub(r"\(.*?\)|（.*?）", "", start_row['event_name']).strip()
                reg_row = c.execute("""
                    SELECT id, event_name, team_name, name, group_name, gender FROM registrations 
                    WHERE name=? AND team_name=? AND group_name=? AND (event_name LIKE ? OR event_name=?)
                """, (start_row['name'], start_row['team_name'], start_row['group_name'], f"%{clean_name}%", start_row['event_name'])).fetchone()
                
                if reg_row:
                    row = reg_row
                    target_reg_id = reg_row['id']
                else:
                    row = start_row
            else:
                conn.close()
                return jsonify({"status": "error", "msg": f"未找到对应的运动员记录(ID: {reg_id})，无法保存"})

        event_name = row['event_name']
        team_name = row['team_name']
        name = row['name']
        group_name = row['group_name']
        gender = row['gender'] if 'gender' in row.keys() else ''
        formatted_score = raw_val

        is_field = False
        if raw_val:
            field_keywords = ['跳', '投', '掷', '铅球', '实心球', '标枪', '铁饼', '球', '引体', '仰卧']
            clean_core = re.sub(r"\(.*?\)|（.*?）", "", event_name).strip()
            
            cfg = c.execute("SELECT type FROM cfg_events WHERE name=?", (clean_core,)).fetchone()
            if cfg and (cfg['type'] == '田赛' or '田' in str(cfg['type'])): 
                is_field = True
            elif any(kwd in event_name for kwd in field_keywords): 
                is_field = True

            if is_field:
                formatted_score = raw_val.replace(':', '.').replace('：', '.')
                if formatted_score.count('.') > 1:
                    parts = formatted_score.split('.')
                    formatted_score = f"{parts[0]}.{parts[1]}"
            else:
                if ':' in raw_val or '：' in raw_val:
                    formatted_score = raw_val.replace('：', ':')
                elif raw_val.count('.') == 2:
                    parts = raw_val.split('.')
                    formatted_score = f"{parts[0]}:{parts[1]}.{parts[2]}"
                else:
                    formatted_score = raw_val

        try: c.execute("ALTER TABLE registrations ADD COLUMN attempts_json TEXT DEFAULT ''")
        except Exception: pass
        try: c.execute("ALTER TABLE start_list ADD COLUMN attempts_json TEXT DEFAULT ''")
        except Exception: pass

        is_relay = re.search(r'4[xX*×]|接力', event_name) is not None
        clean_name = re.sub(r"\(.*?\)|（.*?）", "", event_name).strip()
        
        if is_relay:
            c.execute("""
                UPDATE registrations 
                SET score = ?, attempts_json = ? 
                WHERE team_name = ? AND group_name = ?
                  AND (event_name = ? OR event_name = ? OR event_name LIKE ? || '%')
            """, (formatted_score, attempts_json, team_name, group_name, event_name, clean_name, clean_name))
        else:
            c.execute("UPDATE registrations SET score = ?, attempts_json = ? WHERE id = ?", 
                      (formatted_score, attempts_json, target_reg_id))

        c.execute("""
            UPDATE start_list SET score = ?, attempts_json = ? 
            WHERE name = ? AND team_name = ? AND group_name = ? AND event_name = ?
        """, (formatted_score, attempts_json, name, team_name, group_name, event_name))
            
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()

    # 仅径赛预赛触发自动晋级（纯 4 空格缩进）
    if not is_field and ('预赛' in event_name or 'prelim' in event_name.lower()):
        try:
            check_and_auto_publish_finals(group_name, event_name, gender)
        except Exception:
            pass

    return jsonify({"status": "success", "msg": "已保存", "new_score": formatted_score})
@app.route('/api/trigger_auto_finals', methods=['POST'])
def trigger_auto_finals():
    data = request.json or {}
    g_name = data.get('group_name')
    e_name = data.get('event_name')
    gen = data.get('gender')
    check_and_auto_publish_finals(g_name, e_name, gen)
    return jsonify({"status": "success", "msg": "已触发决赛名单核算与发布！"})

@app.route('/api/direct_publish_announcement_finals', methods=['POST'])
def direct_publish_announcement_finals():
    data = request.json or {}
    g_name = data.get('group_name', '')
    raw_event = data.get('event_name', '')
    gender = data.get('gender', '')
    qualifiers = data.get('qualifiers', [])

    if not qualifiers:
        return jsonify({"status": "error", "msg": "出线名单为空，无法发布！"})

    clean_core = re.sub(r'\(.*?\)|（.*?）|预赛|决赛', '', raw_event).strip()
    clean_gender = '女' if '女' in gender else ('男' if '男' in gender else gender)
    final_event_name = f"{clean_core} (决赛)"

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")

        g_info = c.execute("SELECT id, name FROM cfg_groups WHERE name=?", (g_name,)).fetchone()
        gid = g_info['id'] if g_info else 0
        actual_g_name = g_info['name'] if g_info else g_name

        dummy_meta = c.execute("""
            SELECT est_time, time_index, total_lanes 
            FROM start_list 
            WHERE group_name = ?
              AND gender = ?
              AND (event_name LIKE ? || '%决赛%' OR event_name = ?)
            ORDER BY time_index DESC LIMIT 1
        """, (actual_g_name, clean_gender, clean_core, final_event_name)).fetchone()

        base_est = dummy_meta['est_time'] if (dummy_meta and dummy_meta['est_time']) else '第2天下午 14:30'
        base_tidx = dummy_meta['time_index'] if (dummy_meta and dummy_meta['time_index'] is not None) else 500
        total_lanes = dummy_meta['total_lanes'] if dummy_meta else 8

        c.execute("""
            DELETE FROM start_list 
            WHERE group_name = ?
              AND gender = ?
              AND (event_name LIKE ? || '%决赛%' OR event_name = ?)
        """, (actual_g_name, clean_gender, clean_core, final_event_name))

        c.execute("""
            DELETE FROM registrations 
            WHERE group_name = ?
              AND gender = ?
              AND (event_name LIKE ? || '%决赛%' OR event_name = ?)
        """, (actual_g_name, clean_gender, clean_core, final_event_name))

        cfg_row = c.execute("SELECT limit_count FROM cfg_events WHERE name=? OR name LIKE ?", 
                            (clean_core, f"%{clean_core}%")).fetchone()
        track_lanes = 8
        if cfg_row and cfg_row['limit_count']:
            try: track_lanes = int(cfg_row['limit_count'])
            except Exception: track_lanes = 8

        lane_presets = {
            8: [4, 5, 3, 6, 2, 7, 1, 8],
            6: [3, 4, 2, 5, 1, 6],
            4: [2, 3, 1, 4]
        }
        lane_order = lane_presets.get(track_lanes, [4, 5, 3, 6, 2, 7, 1, 8])

        for idx, q in enumerate(qualifiers):
            assigned_lane = str(lane_order[idx] if idx < len(lane_order) else (idx + 1))
            
            team_id = 0
            t_row = c.execute("SELECT id FROM cfg_teams WHERE name=?", (q.get('team_name', ''),)).fetchone()
            if t_row: team_id = t_row['id']

            c.execute("""
                INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')
            """, (gid, actual_g_name, team_id, q.get('team_name', ''), q.get('name', ''), clean_gender, q.get('bib', ''), final_event_name))

            c.execute("""
                INSERT INTO start_list (group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field, score, checked_in, is_started)
                VALUES (?, ?, ?, '1', ?, ?, ?, ?, 'sprint', ?, ?, ?, 0, '', 0, 0)
            """, (actual_g_name, final_event_name, clean_gender, assigned_lane, q.get('bib', ''), q.get('name', ''), q.get('team_name', ''), total_lanes, base_est, base_tidx))

        conn.commit()
        return jsonify({"status": "success", "msg": f"🎉 成功发布 {len(qualifiers)} 人进入决赛！"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()

@app.route('/api/publish_finals', methods=['POST'])
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
        existing_slots = c.execute("""
            SELECT id, est_time, time_index, total_lanes, type, is_field, heat, lane
            FROM start_list 
            WHERE group_name = ? AND gender = ? 
              AND (event_name = ? OR event_name = ? OR event_name LIKE ? || '%决赛%')
            ORDER BY CAST(heat AS INTEGER) ASC, CAST(lane AS INTEGER) ASC
        """, (g_name, gender, display_name, f"{clean_core} (决赛)", clean_core)).fetchall()

        c.execute("DELETE FROM registrations WHERE group_name=? AND event_name=? AND gender=?", (g_name, display_name, gender))

        if existing_slots and len(existing_slots) > 0:
            base_est = existing_slots[0]['est_time'] or '第2天下午 14:30'
            base_tidx = existing_slots[0]['time_index'] or 100
            
            c.execute("DELETE FROM start_list WHERE group_name=? AND gender=? AND (event_name=? OR event_name=? OR event_name LIKE ? || '%决赛%')", 
                      (g_name, gender, display_name, f"{clean_core} (决赛)", clean_core))

            for i, ath in enumerate(athletes):
                lane = str(ath.get('finalLane', i + 1))
                heat = '1'
                
                c.execute("""INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, score)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')""", 
                          (gid, g_name, ath.get('team_id', 0), ath.get('team_name', ''), ath.get('name', ''), gender, ath.get('bib', ''), display_name))
                
                c.execute("""INSERT INTO start_list (group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field, score, checked_in, is_started)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sprint', 8, ?, ?, 0, '', 0, 0)""",
                          (g_name, display_name, gender, heat, lane, ath.get('bib', ''), ath.get('name', ''), ath.get('team_name', ''), base_est, base_tidx))
        else:
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
                
                c.execute("""INSERT INTO start_list (group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field, score, checked_in, is_started)
                             VALUES (?, ?, ?, '1', ?, ?, ?, ?, 'sprint', 8, ?, ?, 0, '', 0, 0)""",
                          (g_name, display_name, gender, lane, ath.get('bib', ''), ath.get('name', ''), ath.get('team_name', ''), base_est, base_tidx))

        conn.commit()
        return jsonify({"status": "success", "msg": "决赛编排与赛程时间已完美同步发布！"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()

@app.route('/api/manage_team_passwords', methods=['POST'])
@login_required('admin')
def manage_team_passwords():
    action = request.json.get('action')
    conn = get_db_connection()
    c = conn.cursor()

    if action == 'generate':
        teams = set()
        try:
            for r in c.execute("SELECT name FROM cfg_teams").fetchall(): teams.add(r['name'])
            for r in c.execute("SELECT DISTINCT team_name FROM registrations WHERE team_name != ''").fetchall(): teams.add(r['team_name'])
            
            for team in teams:
                if not c.execute("SELECT 1 FROM team_auth WHERE team_name=?", (team,)).fetchone():
                    new_pass = ''.join(random.choices(string.digits, k=6))
                    c.execute("INSERT INTO team_auth (team_name, password) VALUES (?, ?)", (team, new_pass))
            conn.commit()
        except Exception:
            pass

    query = """
        SELECT 
            IFNULL(g.name, '未分配组别') as group_name, 
            ta.team_name, 
            ta.password
        FROM team_auth ta
        LEFT JOIN cfg_teams t ON ta.team_name = t.name
        LEFT JOIN cfg_groups g ON t.group_id = g.id
        ORDER BY g.name, ta.team_name
    """
    rows = c.execute(query).fetchall()
    conn.close()
    return jsonify([{'group': r['group_name'], 'team': r['team_name'], 'password': r['password']} for r in rows])

@app.route('/api/generate_finals_list', methods=['POST'])
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
        
        return jsonify({
            "status": "success",
            "final_event_name": final_display_name, 
            "group_name": g_name,
            "gender": gender,
            "athletes": athletes[:top_n]
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
    data = {
        "groups": [dict(r) for r in c.execute("SELECT * FROM cfg_groups").fetchall()],
        "teams": [dict(r) for r in c.execute("SELECT * FROM cfg_teams").fetchall()],
        "events": [dict(r) for r in c.execute("SELECT * FROM cfg_events").fetchall()],
        "config": {r['key']: r['value'] for r in c.execute("SELECT * FROM sys_config").fetchall()},
        "registrations": [dict(r) for r in c.execute("SELECT * FROM registrations").fetchall()]
    }
    conn.close()
    mem = BytesIO()
    mem.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    mem.seek(0)
    return send_file(mem, mimetype='application/json', as_attachment=True, download_name=f'运动会系统备份_{datetime.now().strftime("%Y%m%d%H%M")}.json')

@app.route('/api/import_system', methods=['POST'])
@login_required('admin')
def import_system():
    if 'file' not in request.files: return jsonify({"status": "error", "msg": "未上传文件"})
    file = request.files['file']
    try:
        data = json.load(file)
        conn = get_db_connection()
        c = conn.cursor()
        
        c.execute("DELETE FROM cfg_groups")
        c.executemany("INSERT INTO cfg_groups (id, name, prefix) VALUES (:id, :name, :prefix)", data.get('groups', []))
        
        c.execute("DELETE FROM cfg_teams")
        c.executemany("INSERT INTO cfg_teams (id, group_id, name, leader) VALUES (:id, :group_id, :name, :leader)", data.get('teams', []))
        
        c.execute("DELETE FROM cfg_events")
        c.executemany("INSERT INTO cfg_events (id, name, type, gender, score_rule, record, record_bonus, is_double_score, need_lane, has_prelim, is_relay, limit_count, allowed_groups) VALUES (:id, :name, :type, :gender, :score_rule, :record, :record_bonus, :is_double_score, :need_lane, :has_prelim, :is_relay, :limit_count, :allowed_groups)", data.get('events', []))
        
        c.execute("DELETE FROM sys_config")
        c.executemany("INSERT INTO sys_config (key, value) VALUES (?, ?)", [(k,v) for k,v in data.get('config', {}).items()])
        
        c.execute("DELETE FROM registrations")
        c.executemany("INSERT INTO registrations (id, group_id, group_name, team_id, team_name, name, gender, bib, event_name, score, rank, lane, heat, submit_time) VALUES (:id, :group_id, :group_name, :team_id, :team_name, :name, :gender, :bib, :event_name, :score, :rank, :lane, :heat, :submit_time)", data.get('registrations', []))
        
        conn.commit()
        return jsonify({"status": "success", "msg": "✅ 备份数据恢复成功！"})
    except Exception as e: 
        return jsonify({"status": "error", "msg": "恢复失败: " + str(e)})
    finally: 
        conn.close()

@app.route('/api/export_registrations')
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
def import_registrations():
    if 'file' not in request.files: return jsonify({"status": "error", "msg": "未上传文件"})
    file = request.files['file']
    if not file.filename.endswith('.csv'): return jsonify({"status": "error", "msg": "请上传 .csv 文件"})

    try:
        stream = StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
        csv_input = csv.reader(stream)
        next(csv_input, None)
        
        conn = get_db_connection() 
        c = conn.cursor()
        
        groups_map = {row['name']: row['id'] for row in c.execute("SELECT id, name FROM cfg_groups").fetchall()}
        teams_map = {row['name']: row['id'] for row in c.execute("SELECT id, name FROM cfg_teams").fetchall()}
        event_types = {row['name']: row['type'] for row in c.execute("SELECT name, type FROM cfg_events").fetchall()}
        
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

            event_list = [item.strip() for col in row[5:] for item in col.replace('，', ',').split(',') if item.strip()]
            unique_events = list(set(event_list))

            for sub_evt in unique_events:
                exists = c.execute("SELECT 1 FROM registrations WHERE team_id=? AND name=? AND event_name=?", (tid, name, sub_evt)).fetchone()
                if exists: continue
                
                evt_type = event_types.get(sub_evt)
                is_fun = evt_type and ('趣味' in str(evt_type))
                
                if not is_fun:
                    evt_meta = c.execute("SELECT is_relay, gender FROM cfg_events WHERE name=?", (sub_evt,)).fetchone()
                    is_relay = to_bool_str(evt_meta['is_relay']) == '1' if evt_meta else False
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
                
                is_new_athlete = not c.execute("SELECT 1 FROM registrations WHERE team_id=? AND name=?", (tid, name)).fetchone()
                if is_new_athlete:
                     curr_team_total = c.execute("SELECT COUNT(DISTINCT name) FROM registrations WHERE team_id=?", (tid,)).fetchone()[0]
                     if curr_team_total >= MAX_TOTAL: 
                         continue 

                c.execute('''INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, submit_time) 
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
                          (gid, g_name, tid, t_name, name, gender, bib, sub_evt, now_str))
                success_count += 1

        conn.commit()
        return jsonify({"status": "success", "msg": f"✅ 成功导入 {success_count} 条记录！"})
    except Exception as e:
        if 'conn' in locals(): conn.rollback()
        return jsonify({"status": "error", "msg": "导入失败: " + str(e)})
    finally:
        if 'conn' in locals(): conn.close()

# 数据库维护与补丁
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS cfg_groups (id INTEGER PRIMARY KEY, name TEXT, prefix TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS cfg_teams (id INTEGER PRIMARY KEY, group_id INTEGER, name TEXT, leader TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS cfg_events (id INTEGER PRIMARY KEY, name TEXT, type TEXT, gender TEXT, score_rule TEXT, record TEXT, record_bonus TEXT, is_double_score BOOLEAN, need_lane BOOLEAN, has_prelim BOOLEAN, is_relay BOOLEAN, limit_count INTEGER, allowed_groups TEXT DEFAULT '')''')
    c.execute('''CREATE TABLE IF NOT EXISTS sys_config (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS team_auth (id INTEGER PRIMARY KEY AUTOINCREMENT, team_name TEXT, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS registrations (id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER, group_name TEXT, team_id INTEGER, team_name TEXT, name TEXT, gender TEXT, bib TEXT, event_name TEXT, score TEXT DEFAULT '', rank TEXT DEFAULT '', lane TEXT DEFAULT '', heat TEXT DEFAULT '', submit_time TEXT)''')
    conn.commit()
    conn.close()

def upgrade_records():
    conn = get_db_connection()
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
    conn.close()

def force_sync_and_upgrade_db():
    conn = get_db_connection()
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
    
   # 规范缩进后的完整片段：
    for col, t in [
        ("total_lanes", "INTEGER DEFAULT 8"),
        ("est_time", "TEXT DEFAULT ''"),
        ("time_index", "INTEGER DEFAULT 0"),
        ("is_field", "INTEGER DEFAULT 0"),
        ("score", "TEXT DEFAULT ''"),
        ("checked_in", "INTEGER DEFAULT 0"),
        ("is_started", "INTEGER DEFAULT 0")
    ]:
        try:
            c.execute(f"ALTER TABLE start_list ADD COLUMN {col} {t}")
        except Exception:
            pass

    try: c.execute("ALTER TABLE registrations ADD COLUMN attempts_json TEXT DEFAULT ''")
    except Exception: pass
    try: c.execute("ALTER TABLE start_list ADD COLUMN attempts_json TEXT DEFAULT ''")
    except Exception: pass   
    try: c.execute("ALTER TABLE cfg_events ADD COLUMN qualify_count INTEGER DEFAULT 8")
    except Exception: pass
    try: c.execute("ALTER TABLE cfg_teams ADD COLUMN coach TEXT DEFAULT ''")
    except Exception: pass
    try: c.execute("ALTER TABLE cfg_teams ADD COLUMN phone TEXT DEFAULT ''")
    except Exception: pass
    
    try:
        c.execute("""
            UPDATE registrations 
            SET score = (
                SELECT s.score FROM start_list s 
                WHERE s.name = registrations.name 
                  AND s.team_name = registrations.team_name 
                  AND s.group_name = registrations.group_name 
                  AND s.event_name = registrations.event_name 
                  AND s.score != ''
                LIMIT 1
            )
            WHERE EXISTS (
                SELECT 1 FROM start_list s 
                WHERE s.name = registrations.name 
                  AND s.team_name = registrations.team_name 
                  AND s.group_name = registrations.group_name 
                  AND s.event_name = registrations.event_name 
                  AND s.score != ''
            )
        """)
    except Exception:
        pass

    conn.commit()
    conn.close()

@app.route('/api/get_group_records', methods=['POST'])
def get_group_records():
    data = request.json or {}
    g_name = data.get('group_name')
    conn = get_db_connection()
    c = conn.cursor()
    try:
        rows = c.execute("SELECT event_name, gender, records_json FROM cfg_group_records WHERE group_name=?", (g_name,)).fetchall()
        res = {}
        for r in rows:
            res[f"{r['event_name']}_{r['gender']}"] = json.loads(r['records_json'])
        return jsonify(res)
    finally:
        conn.close()

@app.route('/api/save_group_records', methods=['POST'])
def save_group_records():
    data = request.json or {}
    g_name = data.get('group_name')
    records_dict = data.get('records', {})
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM cfg_group_records WHERE group_name=?", (g_name,))
        for key, rec_list in records_dict.items():
            parts = key.split('_')
            e_name = parts[0]
            gender = parts[1] if len(parts)>1 else ''
            c.execute("INSERT INTO cfg_group_records (group_name, event_name, gender, records_json) VALUES (?, ?, ?, ?)",
                      (g_name, e_name, gender, json.dumps(rec_list)))
        conn.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()

@app.route('/api/analyze_physical_data', methods=['POST'])
def analyze_physical_data():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        sql = """
            SELECT group_name, team_name, gender, event_name, score
            FROM registrations
            WHERE score IS NOT NULL AND score != ''
        """
        rows = c.execute(sql).fetchall()
        
        cfgs = {r['name']: dict(r) for r in c.execute("SELECT name, type FROM cfg_events").fetchall()}
        grade_analysis = {}

        for r in rows:
            g_name = r['group_name'] or '未知年级'
            t_name = r['team_name'] or '未知班级'
            gen = r['gender']
            raw_evt = r['event_name']
            
            core_evt = re.sub(r"\(.*?\)|（.*?）|决赛|预赛|男子|女子|混合|第\d+组|场地\d+", "", raw_evt).strip()
            if not core_evt:
                continue

            sec_val = parse_time_to_seconds(r['score'])
            if sec_val <= 0:
                continue

            is_field = False
            cfg = cfgs.get(core_evt)
            if cfg and (cfg.get('type') == '田赛' or '田' in str(cfg.get('type'))):
                is_field = True
            elif any(k in core_evt for k in ['跳', '投', '铅球', '实心球', '标枪', '铁饼', '引体', '仰卧']):
                is_field = True

            if g_name not in grade_analysis:
                grade_analysis[g_name] = {"events": {}, "teams": {}}

            if core_evt not in grade_analysis[g_name]["events"]:
                grade_analysis[g_name]["events"][core_evt] = {
                    "男": [],
                    "女": [],
                    "is_field": is_field
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
                is_field = evt_data["is_field"]

                event_summary[evt_name] = {
                    "is_field": is_field,
                    "male_avg": round(sum(m_list) / len(m_list), 2) if m_list else 0,
                    "male_count": len(m_list),
                    "male_best": (max(m_list) if is_field else min(m_list)) if m_list else 0,
                    "female_avg": round(sum(f_list) / len(f_list), 2) if f_list else 0,
                    "female_count": len(f_list),
                    "female_best": (max(f_list) if is_field else min(f_list)) if f_list else 0,
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
def export_handbook_word():
    conn = get_db_connection()
    c = conn.cursor()
    try:
        cfg_rows = c.execute("SELECT key, value FROM sys_config").fetchall()
        config = {r['key']: r['value'] for r in cfg_rows}
        title = config.get('title', '田径运动会')

        groups = [dict(r) for r in c.execute("SELECT * FROM cfg_groups ORDER BY id ASC").fetchall()]
        teams = [dict(r) for r in c.execute("SELECT * FROM cfg_teams ORDER BY group_id ASC, id ASC").fetchall()]
        events = [dict(r) for r in c.execute("SELECT * FROM cfg_events ORDER BY id ASC").fetchall()]

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

        schedule = [dict(r) for r in c.execute("""
            SELECT id, group_name, event_name, gender, heat, lane, bib, name, team_name, type, est_time, time_index, is_field
            FROM start_list
            ORDER BY time_index ASC, CAST(heat AS INTEGER) ASC, CAST(lane AS INTEGER) ASC
        """).fetchall()]

        def parse_unit_date(est_time_str):
            if not est_time_str:
                return "第一单元 (比赛日 上午)", "08:30"
            m = re.search(r"第(\d+)天(上午|下午)?\s*(\d+[:：]\d+)?", est_time_str)
            digits_cn = {"1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六"}
            if m:
                d_num = m.group(1)
                u_cn = digits_cn.get(d_num, d_num)
                ampm = m.group(2) or "上午"
                t_str = m.group(3) or "09:00"
                date_val = ""
                try:
                    c_date = c.execute("SELECT value FROM system_settings WHERE key='start_date'").fetchone()
                    if c_date and c_date[0]:
                        base_d = datetime.strptime(c_date[0], "%Y-%m-%d")
                        cur_d = base_d + timedelta(days=int(d_num)-1)
                        date_val = cur_d.strftime("%Y年%m月%d日")
                except Exception:
                    pass
                if not date_val:
                    date_val = datetime.now().strftime("%Y年%m月%d日")

                return f"第{u_cn}单元 ({date_val}  {ampm})", t_str.replace('：', ':')
            return "第一单元 (比赛日 上午)", "09:00"

        team_athletes_map = {}
        for t in teams:
            team_athletes_map[t['name']] = {
                'leader': t.get('leader', ''),
                'groups': {}
            }

        seen_ath = set()
        for r in regs:
            ath_key = (r['team_name'], r['name'], r['group_name'], r['gender'])
            if ath_key in seen_ath:
                continue
            seen_ath.add(ath_key)

            t_n = r['team_name']
            if t_n not in team_athletes_map:
                team_athletes_map[t_n] = {'leader': '', 'groups': {}}

            prefix = f"{'男' if r['gender'] == '男' else '女'}子"
            g_clean = r['group_name'].replace('男子', '').replace('女子', '')
            g_label = f"{prefix}{g_clean}"

            if g_label not in team_athletes_map[t_n]['groups']:
                team_athletes_map[t_n]['groups'][g_label] = []

            raw_bib = str(r['bib'] or '').strip()
            bib_str = raw_bib.zfill(4) if raw_bib.isdigit() else raw_bib
            team_athletes_map[t_n]['groups'][g_label].append({
                'bib': bib_str,
                'name': r['name']
            })

        teams_html = """
        <h1 style='text-align:center; font-size:26pt; font-weight:bold; letter-spacing:4px; margin-bottom:10pt;'>代表队名单</h1>
        <div style='text-align:center; font-size:16pt; font-weight:bold; margin-bottom:25pt;'>(参赛单位)</div>
        """

        for t_name, t_info in team_athletes_map.items():
            if not t_info['groups']:
                continue
            leader_txt = t_info['leader'] if t_info['leader'] else "—"
            teams_html += f"""
            <div style='margin-bottom:28pt; page-break-inside:avoid;'>
                <div style='text-align:center; font-size:18pt; font-weight:bold; margin-bottom:12pt;'>{t_name}</div>
                <div style='font-size:11pt; line-height:1.6;'>领队：{leader_txt}</div>
                <div style='font-size:11pt; line-height:1.6; margin-bottom:8pt;'>教练：{leader_txt}</div>
            """
            for g_label, ath_list in t_info['groups'].items():
                teams_html += f"""
                <div style='font-size:12pt; font-weight:bold; margin-top:8pt; margin-bottom:4pt;'>{g_label}</div>
                <table style='width:100%; border-collapse:collapse; font-size:10.5pt; table-layout:fixed; border:none; margin-bottom:8pt;'>
                """
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

        schedule_unit_map = {}
        for s in schedule:
            if not s['name'] or '待定' in s['name'] or s['team_name'] == '预赛出线':
                continue
            u_title, time_val = parse_unit_date(s.get('est_time', ''))
            is_field = 1 if (s.get('is_field') == 1 or any(k in s['event_name'] for k in ['跳', '投', '球', '掷'])) else 0
            category = "田  赛" if is_field else "径  赛"

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

        unit_schedule_tree = {}
        for (u_title, cat, g_name, e_name, gen), info in schedule_unit_map.items():
            if u_title not in unit_schedule_tree:
                unit_schedule_tree[u_title] = {'径  赛': [], '田  赛': []}

            clean_core = re.sub(r'[\(（].*?[\)）]', '', e_name).replace('预赛', '').replace('决赛', '').strip()
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

        schedule_html = """
        <h1 style='text-align:center; font-size:26pt; font-weight:bold; letter-spacing:10px; margin-bottom:12pt;'>竞 赛 日 程</h1>
        """

        for u_title, cats in unit_schedule_tree.items():
            schedule_html += f"""
            <div style='text-align:center; font-size:15pt; font-weight:bold; margin-top:20pt; margin-bottom:8pt;'>
                <u>{u_title}</u>
            </div>
            """
            for cat_title in ['径  赛', '田  赛']:
                items = cats[cat_title]
                if not items:
                    continue
                items.sort(key=lambda x: x['time_index'])

                schedule_html += f"""
                <div style='text-align:center; font-size:14pt; font-weight:bold; margin-top:10pt; margin-bottom:6pt;'>{cat_title}</div>
                <table style='width:100%; border-collapse:collapse; text-align:center; font-size:10.5pt; margin-bottom:16pt; border-top:1.5pt solid #000; border-bottom:1.5pt solid #000;'>
                    <tr style='height:28px; border-bottom:1pt solid #000;'>
                        <th style='width:45px; border:none;'>序号</th>
                        <th style='width:75px; border:none;'>比赛时间</th>
                        <th style='border:none; text-align:left; padding-left:10px;'>项目名称</th>
                        <th style='width:60px; border:none;'>赛次</th>
                        <th style='width:60px; border:none;'>人数</th>
                        <th style='width:60px; border:none;'>组数</th>
                        <th style='width:140px; border:none; text-align:left;'>录取原则</th>
                    </tr>
                """
                for idx, row in enumerate(items, 1):
                    schedule_html += f"""
                    <tr style='height:26px; border:none;'>
                        <td style='border:none;'>{idx}</td>
                        <td style='border:none; font-family:\"Times New Roman\";'>{row['time']}</td>
                        <td style='border:none; text-align:left; padding-left:10px;'>{row['event_full_name']}</td>
                        <td style='border:none;'>{row['round']}</td>
                        <td style='border:none;'>{row['count_str']}</td>
                        <td style='border:none;'>{row['heats_str']}</td>
                        <td style='border:none; text-align:left;'>{row['qualify']}</td>
                    </tr>
                    """
                schedule_html += "</table>"

        heats_task_map = {}
        for s in schedule:
            if not s['name'] or '待定' in s['name'] or s['team_name'] == '预赛出线':
                continue
            u_title, _ = parse_unit_date(s.get('est_time', ''))
            is_f = 1 if (s.get('is_field') == 1 or any(k in s['event_name'] for k in ['跳', '投', '球', '掷'])) else 0
            category = "田赛" if is_f else "径赛"

            ek = (u_title, category, s['group_name'], s['event_name'], s['gender'])
            if ek not in heats_task_map:
                heats_task_map[ek] = {}
            h_no = str(s['heat'])
            if h_no not in heats_task_map[ek]:
                heats_task_map[ek][h_no] = []
            heats_task_map[ek][h_no].append(s)

        groups_html = f"""
        <h1 style='text-align:center; font-size:26pt; font-weight:bold; letter-spacing:4px; margin-bottom:12pt;'>竞赛分组名单</h1>
        """

        units_ordered = sorted(list(set([k[0] for k in heats_task_map.keys()])))

        for u_title in units_ordered:
            groups_html += f"""
            <div style='text-align:center; font-size:15pt; font-weight:bold; margin-top:20pt; margin-bottom:12pt;'>
                <u>{u_title}</u>
            </div>
            """
            for cat_type in ["径赛", "田赛"]:
                groups_html += f"<div style='font-size:15pt; font-weight:bold; margin-top:14pt; margin-bottom:8pt;'>{cat_type}</div>"
                
                target_keys = [k for k in heats_task_map.keys() if k[0] == u_title and k[1] == cat_type]
                item_idx = 1

                for (u_t, cat, g_name, e_name, gen) in target_keys:
                    clean_core = re.sub(r'[\(（].*?[\)）]', '', e_name).replace('预赛', '').replace('决赛', '').strip()
                    round_str = "预赛" if '预赛' in e_name else "决赛"
                    prefix = f"{'男' if gen == '男' else '女'}子"
                    g_clean = g_name.replace('男子', '').replace('女子', '')
                    full_event_title = f"{prefix}{g_clean}{clean_core}{round_str}"

                    heat_dict = heats_task_map[(u_t, cat, g_name, e_name, gen)]
                    total_p = sum(len(lst) for lst in heat_dict.values())
                    total_h = len(heat_dict)

                    unit_label = "人" if ('接力' not in e_name and '4x' not in e_name.lower()) else "队"

                    if cat_type == "径赛":
                        groups_html += f"""
                        <div style='font-size:13pt; font-weight:bold; margin-top:14pt; margin-bottom:6pt; page-break-inside:avoid;'>
                            {item_idx} . {full_event_title}  共{total_p}{unit_label}  共{total_h}组
                        </div>
                        """
                        item_idx += 1

                        for h_no in sorted(heat_dict.keys(), key=lambda x: int(x)):
                            h_athletes = heat_dict[h_no]
                            h_athletes.sort(key=lambda x: int(x['lane']) if str(x['lane']).isdigit() else 99)
                            
                            t_display = "09:00"
                            m = re.search(r"\d+[:：]\d+", h_athletes[0].get('est_time', ''))
                            if m:
                                t_display = m.group(0).replace('：', ':')

                            lane_map = {int(a['lane']): a for a in h_athletes if str(a['lane']).isdigit()}
                            max_lane = max(8, max(lane_map.keys()) if lane_map else 8)

                            lane_th_row = ""
                            bib_td_row = ""
                            name_td_row = ""
                            team_td_row = ""

                            is_relay = ('接力' in e_name or '4x' in e_name.lower())

                            for l_idx in range(1, max_lane + 1):
                                ath = lane_map.get(l_idx)
                                lane_th_row += f"<td style='border:none; width:{100/max_lane:.2f}%; text-align:center;'>{l_idx if ath else ''}</td>"
                                if ath:
                                    raw_b = str(ath.get('bib') or '').strip()
                                    b_txt = raw_b.zfill(4) if raw_b.isdigit() else raw_b
                                    bib_td_row += f"<td style='border:none; text-align:center; font-family:\"Times New Roman\";'>{b_txt if not is_relay else ''}</td>"
                                    name_td_row += f"<td style='border:none; text-align:center; font-weight:bold;'>{ath['name'] if not is_relay else ''}</td>"
                                    team_td_row += f"<td style='border:none; text-align:center; font-size:9pt;'>{ath['team_name']}</td>"
                                else:
                                    bib_td_row += "<td style='border:none;'></td>"
                                    name_td_row += "<td style='border:none;'></td>"
                                    team_td_row += "<td style='border:none;'></td>"

                            groups_html += f"""
                            <div style='margin-bottom:12pt; page-break-inside:avoid;'>
                                <div style='font-size:11pt; font-weight:bold; margin-bottom:3pt;'>第{h_no}组  {t_display}</div>
                                <table style='width:100%; border-collapse:collapse; font-size:10pt; table-layout:fixed; border-top:1pt solid #000; border-bottom:1pt solid #000; margin-bottom:8pt;'>
                                    <tr style='height:20px; border-bottom:0.5pt solid #888;'>{lane_th_row}</tr>
                                    {f"<tr style='height:20px;'>{bib_td_row}</tr>" if not is_relay else ""}
                                    {f"<tr style='height:20px;'>{name_td_row}</tr>" if not is_relay else ""}
                                    <tr style='height:20px;'>{team_td_row}</tr>
                                </table>
                            </div>
                            """
                    else:
                        h_athletes = heat_dict[list(heat_dict.keys())[0]]
                        h_athletes.sort(key=lambda x: int(x['lane']) if str(x['lane']).isdigit() else 99)

                        t_display = "14:30"
                        m = re.search(r"\d+[:：]\d+", h_athletes[0].get('est_time', ''))
                        if m:
                            t_display = m.group(0).replace('：', ':')

                        groups_html += f"""
                        <div style='font-size:13pt; font-weight:bold; margin-top:14pt; margin-bottom:6pt; page-break-inside:avoid;'>
                            {item_idx} . {full_event_title}  共{total_p}人  共{total_h}组  <u>{t_display}</u>
                        </div>
                        """
                        item_idx += 1

                        chunk_size = 8
                        for i in range(0, len(h_athletes), chunk_size):
                            chunk = h_athletes[i:i+chunk_size]

                            th_cells = ""
                            bib_cells = ""
                            name_cells = ""
                            team_cells = ""

                            for seq_idx, ath in enumerate(chunk, start=i+1):
                                raw_b = str(ath.get('bib') or '').strip()
                                b_txt = raw_b.zfill(4) if raw_b.isdigit() else raw_b
                                th_cells += f"<td style='border:none; width:12.5%; text-align:center;'>{seq_idx}</td>"
                                bib_cells += f"<td style='border:none; text-align:center; font-family:\"Times New Roman\";'>{b_txt}</td>"
                                name_cells += f"<td style='border:none; text-align:center; font-weight:bold;'>{ath['name']}</td>"
                                team_cells += f"<td style='border:none; text-align:center; font-size:9pt;'>{ath['team_name']}</td>"

                            for _ in range(chunk_size - len(chunk)):
                                th_cells += "<td style='border:none; width:12.5%;'></td>"
                                bib_cells += "<td style='border:none;'></td>"
                                name_cells += "<td style='border:none;'></td>"
                                team_cells += "<td style='border:none;'></td>"

                            groups_html += f"""
                            <div style='margin-bottom:10pt; page-break-inside:avoid;'>
                                <table style='width:100%; border-collapse:collapse; font-size:10pt; table-layout:fixed; border-top:1pt solid #000; border-bottom:1pt solid #000; margin-bottom:6pt;'>
                                    <tr style='height:20px; border-bottom:0.5pt solid #888;'>{th_cells}</tr>
                                    <tr style='height:20px;'>{bib_cells}</tr>
                                    <tr style='height:20px;'>{name_cells}</tr>
                                    <tr style='height:20px;'>{team_cells}</tr>
                                </table>
                            </div>
                            """

        full_word_html = f"""<html xmlns:o='urn:schemas-microsoft-com:office:office' xmlns:w='urn:schemas-microsoft-com:office:word' xmlns='http://www.w3.org/TR/REC-html40'>
        <head>
            <meta charset='utf-8'>
            <title>{title} 竞赛秩序册</title>
            <style>
                @page {{ size: A4 portrait; margin: 2.5cm 2.0cm; }}
                body {{ font-family: 'SimSun', 'Songti SC', serif; color: #000; line-height: 1.35; }}
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

@app.route('/api/batch_save_events', methods=['POST'])
@login_required('admin')
def batch_save_events():
    data = request.get_json() or {}
    events = data.get('events', [])
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        for col, dtype in [
            ("is_fun", "TEXT DEFAULT '0'"), ("allowed_groups", "TEXT DEFAULT ''"), 
            ("duration", "REAL DEFAULT 5"), ("venue_count", "INTEGER DEFAULT 1"),
            ("qualify_count", "INTEGER DEFAULT 8")
        ]:
            try: c.execute(f"ALTER TABLE cfg_events ADD COLUMN {col} {dtype}")
            except Exception: pass
        
        c.execute("DELETE FROM cfg_events")
        
        for i, e in enumerate(events):
            has_prelim_str = '1' if e.get('hasPrelim') else '0'
            is_relay_str = '1' if e.get('isRelay') else '0'
            need_lane_str = '1' if e.get('needLane') else '0'
            is_fun_str = '1' if e.get('isFun', False) else '0'
            
            limit_val = e.get('limit') if e.get('limit') is not None else 8
            dur_val = e.get('duration') if e.get('duration') is not None else 5
            ven_val = e.get('venueCount') if e.get('venueCount') is not None else 1
            qualify_val = int(e.get('qualifyCount') or 8)
            
            c.execute('''
                INSERT INTO cfg_events 
                (id, name, type, gender, score_rule, record, record_bonus, is_double_score, need_lane, has_prelim, is_relay, limit_count, is_fun, allowed_groups, duration, venue_count, qualify_count)
                VALUES (?, ?, ?, ?, ?, '', ?, '0', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                str(i + 1), e.get('name', ''), e.get('type', ''), e.get('gender', '双性'),
                e.get('scoreRule', '9,7,6,5,4,3,2,1'), e.get('recordBonus', 0), 
                need_lane_str, has_prelim_str, is_relay_str, 
                int(limit_val), is_fun_str,
                str(e.get('allowedGroups', '')),
                float(dur_val), int(ven_val), qualify_val
            ))
            
        conn.commit()
        return jsonify({"success": True, "message": "✅ 项目选用矩阵及录取名次配置已全量保存！"})
    except Exception as ex:
        conn.rollback()
        return jsonify({"success": False, "message": str(ex)})
    finally:
        conn.close()

# 初始化数据库
init_db()
force_sync_and_upgrade_db()
upgrade_records()

# 后台热备份守护线程
def auto_db_backup_task():
    backup_dir = os.path.join(DATA_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    
    while True:
        time.sleep(900)
        try:
            if os.path.exists(DB_FILE):
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                bk_file = os.path.join(backup_dir, f"sports_auto_backup_{stamp}.db")
                
                src = sqlite3.connect(DB_FILE)
                dst = sqlite3.connect(bk_file)
                with dst:
                    src.backup(dst)
                dst.close()
                src.close()
                
                bk_list = sorted([os.path.join(backup_dir, f) for f in os.listdir(backup_dir) if f.endswith('.db')])
                while len(bk_list) > 20:
                    old_file = bk_list.pop(0)
                    try: os.remove(old_file)
                    except Exception: pass
        except Exception as err:
            print(f"⚠️ [自动热备份异常]: {err}")

threading.Thread(target=auto_db_backup_task, daemon=True).start()

if __name__ == '__main__':
    local_ip = get_host_ip()
    print("✅ 启动成功！")
    print(f"👉 领队端: http://{local_ip}:5000/bm")
    print(f"👉 管理端: http://{local_ip}:5000/admin/login")
    print(f"👉 裁判端: http://{local_ip}:5000/referee/login")
    
    app.jinja_env.auto_reload = True
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    serve(app, host='0.0.0.0', port=5000, threads=16)