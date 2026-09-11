from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
import sqlite3
import json
from datetime import datetime
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

# 1. 动态判断运行环境（兼容原生 Python、Docker 容器以及打包后的 .exe）
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

def to_bool_str(val):
    """将各种类型的布尔值统一转换为字符串 '1' 或 '0'"""
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

# 4. 数据库连接基础函数（移至顶部，避免未定义错误）
def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=20) 
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
    except:
        return 0.0

def get_host_ip():
    """获取本机局域网 IP 地址"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

# ============================================================
# 🔒 独立权限拦截器
# ============================================================
def login_required(role_needed):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_role' not in session:
                if role_needed == 'admin': 
                    return redirect('/admin/login')
                elif role_needed == 'referee': 
                    return redirect('/referee/login')
                else: 
                    return redirect('/bm') 
            
            current_role = session['user_role']
            if role_needed == 'admin' and current_role != 'admin': 
                return redirect('/admin/login')
            if role_needed == 'referee' and current_role not in ['admin', 'referee']: 
                return redirect('/referee/login')

            return f(*args, **kwargs)
        return decorated_function
    return decorator

# ============================================================
# 🌐 页面路由
# ============================================================
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

# ============================================================
# 🔑 统一认证 API
# ============================================================
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
        else:
            return jsonify({'status': 'fail', 'msg': '认证失败：登录名或密码错误'})
    elif role_type == 'referee':
        username = data.get('username')
        if username == 'referee' and data.get('password') == REFEREE_PASSWORD:
            session['user_role'] = 'referee'
            return jsonify({'status': 'success', 'redirect': '/referee'})
        else:
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
    else:
        return redirect('/bm')

# ============================================================
# ⚙️ 检录与赛程监控 API
# ============================================================
@app.route('/api/toggle_checkin', methods=['POST'])
def toggle_checkin():
    data = request.json or {}
    start_id = data.get('id')
    # status 允许传入 0 (未检录), 1 (到位), 2 (弃权)
    status = int(data.get('checked_in', 0))
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        
        # 1. 查找当前检录的道次记录
        start_row = c.execute("SELECT id, name, team_name, group_name, event_name, gender FROM start_list WHERE id = ?", (start_id,)).fetchone()
        if not start_row:
            conn.close()
            return jsonify({"status": "error", "msg": "未找到对应的道次记录"})

        # 2. 更新 start_list 检录状态
        c.execute("UPDATE start_list SET checked_in = ? WHERE id = ?", (status, start_id))
        
        # 🌟 3. 核心修复：如果是弃权(status=2)，同步在 registrations 写入弃权标记；取消弃权则抹除
        clean_name = re.sub(r"\(.*?\)|（.*?）", "", start_row['event_name']).strip()
        abandon_mark = '弃权' if status == 2 else ''
        
        # 当标记弃权时，将 registrations 表中的成绩更新为“弃权”，解除未完赛阻塞
        if status == 2:
            c.execute("""
                UPDATE registrations 
                SET score = '弃权' 
                WHERE name = ? AND team_name = ? AND group_name = ? 
                  AND (event_name = ? OR event_name = ? OR event_name LIKE ?)
                  AND (score = '' OR score IS NULL OR score = '弃权')
            """, (start_row['name'], start_row['team_name'], start_row['group_name'], start_row['event_name'], clean_name, f"%{clean_name}%"))
        else:
            # 取消弃权时，如果此前是自动写入的“弃权”，则清空恢复待录入状态
            c.execute("""
                UPDATE registrations 
                SET score = '' 
                WHERE name = ? AND team_name = ? AND group_name = ? 
                  AND (event_name = ? OR event_name = ? OR event_name LIKE ?)
                  AND score = '弃权'
            """, (start_row['name'], start_row['team_name'], start_row['group_name'], start_row['event_name'], clean_name, f"%{clean_name}%"))

        conn.commit()
        conn.close()

        try:
            check_and_auto_publish_finals(
                start_row['group_name'], 
                start_row['event_name'], 
                start_row['gender'] or ''
            )
        except Exception as e:
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
        # 查询分组及关联成绩与检录状态
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
            # 排除弃权（checked_in == 2）的实际参赛人数
            actual_runners = [a for a in t["athletes"] if a["checked_in"] != 2]
            scored_count = sum(1 for a in actual_runners if a["score"] and str(a["score"]).strip() != '')
            checked_count = sum(1 for a in t["athletes"] if a["checked_in"] == 1)
            
            # 🌟 核心状态流转机制
            if len(actual_runners) > 0 and scored_count >= len(actual_runners):
                status = "finished"  # 🏁 终点录完所有有效选手成绩 -> 已结束
            elif t["is_started"] == 1 or scored_count > 0:
                status = "ongoing"   # 🟢 起点发车推送 (is_started==1) 或已有部分成绩 -> 进行中
            elif checked_count > 0:
                status = "pending"   # 🔵 检录有人到位 -> 待开始
            else:
                status = "unchecked" # ⚪ 尚未检录

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
# ============================================================
# ⚙️ 业务功能 API
# ============================================================
@app.route('/api/recalculate_all_points', methods=['POST'])
def recalculate_all_points():
    conn = get_db_connection()
    c = conn.cursor()
    count = 0
    try:
        try: c.execute("ALTER TABLE registrations ADD COLUMN points INTEGER DEFAULT 0")
        except: pass
        try: c.execute("ALTER TABLE registrations ADD COLUMN record_bonus INTEGER DEFAULT 0")
        except: pass

        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE registrations SET points = 0, record_bonus = 0")
        
        groups_genders = c.execute("SELECT DISTINCT group_name, gender FROM registrations WHERE group_name != ''").fetchall()
        all_cfgs = {row['name']: dict(row) for row in c.execute("SELECT * FROM cfg_events").fetchall()}

        for gg in groups_genders:
            g_name, gender = gg['group_name'], gg['gender']
            
            group_records_raw = []
            try: group_records_raw = c.execute("SELECT event_name, gender, records_json FROM cfg_group_records WHERE group_name = ?", (g_name,)).fetchall()
            except: pass
            group_records_map = {}
            for gr in group_records_raw:
                group_records_map[f"{gr['event_name']}_{gr['gender']}"] = json.loads(gr['records_json'])

            rows = c.execute("SELECT DISTINCT event_name FROM registrations WHERE group_name = ? AND gender = ? AND score != ''", (g_name, gender)).fetchall()
            distinct_events = [r['event_name'] for r in rows]
            if not distinct_events: continue

            event_map = {}
            for evt in distinct_events:
                # 🌟 确保清洗正则表达式包含 "场地\d+"，将各场地归一化为纯净项目名
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

                final_list = list(unique_entries.values())
                final_list.sort(key=lambda x: x['_val'], reverse=is_field)
                
                score_rule = cfg.get('score_rule', "9,7,6,5,4,3,2,1") if cfg else "9,7,6,5,4,3,2,1"
                rules = [int(x) for x in score_rule.replace('，',',').split(',') if x.strip().isdigit()]
                is_double = (to_bool_str(cfg.get('is_double_score')) == '1') if cfg else False

                my_records = group_records_map.get(f"{core_name}_{gender}", [])

                current_rank = 1
                for i, item in enumerate(final_list):
                    if i > 0 and item['_val'] != final_list[i-1]['_val']:
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
        import traceback; traceback.print_exc()
        conn.rollback()
        return jsonify({'status': 'error', 'msg': str(e)})
    finally:
        conn.close()

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
    g_name = request.json.get('group_name')
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
        return jsonify([dict(r) for r in rows])
    except:
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
# ============================================================
# 🚩 起点裁判与终点裁判联动 API
# ============================================================
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

        # 🌟 1. 核心互斥锁：检查当前跑道上的那组是否已经录完全部成绩
        c.execute("""CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)""")
        active_row = c.execute("SELECT value FROM system_settings WHERE key='active_track_heat'").fetchone()
        
        if active_row and active_row[0]:
            try:
                cur_active = json.loads(active_row[0])
                # 查询当前在跑道上的一组运动员
                chk_sql = """
                    SELECT s.checked_in, IFNULL(r.score, s.score) as score
                    FROM start_list s
                    LEFT JOIN registrations r 
                        ON s.name = r.name AND s.team_name = r.team_name AND s.group_name = r.group_name 
                        AND (r.event_name = s.event_name OR r.event_name = REPLACE(s.event_name, ' (预赛)', ''))
                    WHERE s.group_name = ? AND s.event_name = ? AND s.gender = ? AND s.heat = ?
                """
                active_ath = c.execute(chk_sql, (cur_active['group_name'], cur_active['event_name'], cur_active['gender'], cur_active['heat'])).fetchall()
                
                # 排除弃权(2)的选手
                valid_ath = [a for a in active_ath if a['checked_in'] != 2]
                unscored_count = sum(1 for a in valid_ath if not a['score'] or str(a['score']).strip() == '')
                
                # 如果还有有效参赛选手没录入成绩，坚决拦截
                if len(valid_ath) > 0 and unscored_count > 0:
                    conn.close()
                    return jsonify({
                        "status": "error", 
                        "msg": f"⚠️ 跑道正忙！终点裁判正在录入【{cur_active['group_name']} {cur_active['event_name']} 第{cur_active['heat']}组】的成绩，尚余 {unscored_count} 人未录完，请稍候！"
                    })
            except Exception as e:
                pass

        # 2. 清洗性别
        clean_gender = '女' if '女' in gender else ('男' if '男' in gender else gender)
        push_time = datetime.now().strftime('%H:%M:%S')

        heat_payload = {
            "group_name": g_name,
            "event_name": e_name,
            "gender": clean_gender,
            "heat": heat,
            "push_time": push_time
        }

        # 3. 写入全局发车状态
        c.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('active_track_heat', ?)", 
                  (json.dumps(heat_payload, ensure_ascii=False),))

        # 4. 更新 start_list 中的已发车标记
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
    """终点与发令裁判轮询：获取跑道状态及是否录完"""
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)""")
        row = c.execute("SELECT value FROM system_settings WHERE key='active_track_heat'").fetchone()
        if not row or not row[0]:
            return jsonify({"status": "success", "data": None, "is_finished": True})

        active_data = json.loads(row[0])
        
        # 检查这组是否已经录完全部成绩
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
    except Exception as e:
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
    except Exception as e:
        import traceback; traceback.print_exc()
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
    except Exception as e:
        import traceback; traceback.print_exc()
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
                    relay_leg = ''  -- 🌟 重置时清空棒次
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
        import traceback; traceback.print_exc()
        return jsonify({"status": "error", "msg": "操作失败: " + str(e)})
    finally:
        conn.close()
        force_sync_and_upgrade_db()

@app.route('/api/export_teams')
def export_teams():
    conn = get_db_connection()
    c = conn.cursor()
    # 🌟 使用 LEFT JOIN，保证所有组别及其下属班级全部导出
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
    output.write('\ufeff')  # 写入 BOM 防止 Excel 打开乱码
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
            # 兼容4列格式（组别, 前缀, 队名, 领队）与旧版3列格式（组别, 队名, 领队）
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

            # 如果组别不存在则自动新建组别
            if g_name not in groups_map:
                new_gid = int(datetime.now().timestamp() * 1000) + random.randint(100, 999)
                c.execute("INSERT INTO cfg_groups (id, name, prefix) VALUES (?, ?, ?)", 
                          (new_gid, g_name, g_prefix))
                groups_map[g_name] = new_gid
                success_groups += 1

            gid = groups_map[g_name]

            # 若填写了代表队名称，则新建或更新代表队
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
    group_stats = c.execute("""
        SELECT group_name, gender, COUNT(DISTINCT name) as count 
        FROM registrations 
        WHERE group_name IS NOT NULL AND name != ''
        GROUP BY group_name, gender
    """).fetchall()
    event_stats = c.execute("""
        SELECT event_name, COUNT(*) as count 
        FROM registrations 
        WHERE event_name != ''
        GROUP BY event_name
    """).fetchall()
    team_engagement = c.execute("""
        SELECT team_name, COUNT(DISTINCT name) as athlete_count 
        FROM registrations 
        GROUP BY team_name 
        ORDER BY athlete_count DESC 
        LIMIT 5
    """).fetchall()
    total_athletes = c.execute("SELECT COUNT(DISTINCT team_name || name) FROM registrations WHERE name != ''").fetchone()[0]
    total_participations = c.execute("SELECT COUNT(*) FROM registrations WHERE event_name != ''").fetchone()[0]
    
    conn.close()
    return jsonify({
        "group_gender": [dict(r) for r in group_stats],
        "events": [dict(r) for r in event_stats],
        "top_teams": [dict(r) for r in team_engagement], 
        "total_athletes": total_athletes,
        "total_participations": total_participations
    })

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
            # 🌟 修复：在 SELECT 语句末尾增加 IFNULL(is_started, 0) as is_started
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
                # 🌟 修复：把数据库字段映射给前端
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
            except: pass
            
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
        import traceback; traceback.print_exc()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()

@app.route('/api/save_relay_legs', methods=['POST'])
def save_relay_legs():
    current_role = session.get('user_role')
    # 🌟 允许领队(team)以及管理员(admin)均可保存接力棒次
    if current_role not in ['team', 'admin']:
        return jsonify({"status": "error", "msg": "登录会话已超时，请刷新页面重新登录代表队账号！"}), 401
        
    data = request.json or {}
    team_id = data.get('team_id')
    event_name = data.get('event_name', '')
    gender = data.get('gender', '')
    legs = data.get('legs', {})
    
    # 领队只能保存本班棒次（管理员不受限）
    if current_role == 'team' and str(team_id) != str(session.get('team_id')):
        return jsonify({"status": "error", "msg": "越权操作：只能提交本班队伍的接力棒次！"}), 403        
    data = request.json or {}
    team_id = data.get('team_id')
    event_name = data.get('event_name', '')
    gender = data.get('gender', '')
    legs = data.get('legs', {})
    
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
        import traceback; traceback.print_exc()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
@app.route('/api/save_config', methods=['POST'])
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
                c.execute("INSERT OR REPLACE INTO cfg_teams (id, group_id, name, leader) VALUES (?, ?, ?, ?)", 
                          (str(t['id']), str(t['groupId']), t['name'], t.get('leader','')))
                  
        if 'events' in data:
            c.execute("DELETE FROM cfg_events")
            for e in data['events']: 
                rule = e.get('scoreRule') or e.get('score_rule') or '9,7,6,5,4,3,2,1'
                rec = e.get('record') or ''
                bonus = e.get('recordBonus') or e.get('record_bonus') or 0
                
                # 兼容前端传入的 0 值，不能直接用 or
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
        import traceback; traceback.print_exc()
        return jsonify({"status": "error", "msg": "保存失败: " + str(e)})
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
    
    if user_role == 'team':
        if str(data.get('team_id')) != str(session.get('team_id')):
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
                        return jsonify({"status": "error", "msg": f"报名通道已关闭！截止时间为：{deadline_row[0].replace('T', ' ')}"})
                except Exception as e:
                    pass
        
        def get_cfg_val(key, default):
            row = c.execute("SELECT value FROM sys_config WHERE key=?", (key,)).fetchone()
            return int(row[0]) if row else default
        
        MAX_PER_EVENT = get_cfg_val('maxPerEvent', 3)
        MAX_TOTAL = get_cfg_val('maxTotal', 20)
        
        team_id = str(data.get('team_id'))
        group_id = str(data.get('group_id'))
        name = data.get('name', '').strip()
        gender = data.get('gender')
        bib = data.get('bib', '').strip()
        selected_events = data.get('events', [])
        
        if not name:
            return jsonify({"status": "error", "msg": "姓名不能为空！"})
        if not selected_events:
            return jsonify({"status": "error", "msg": "请至少选择一个项目！"})

        current_team_count = c.execute("SELECT COUNT(DISTINCT name) FROM registrations WHERE team_id=? AND name!=?", (team_id, name)).fetchone()[0]
        if current_team_count >= MAX_TOTAL:
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
        import traceback; traceback.print_exc()
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
            except:
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
def save_schedule_to_db():
    schedule_data = request.json
    if not schedule_data: 
        return jsonify({"status": "error", "msg": "没有接收到合法的编排名单数据"})
        
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")

        # 🌟 1. 确保表存在后再清理旧编排与发车标记
        c.execute("""
            CREATE TABLE IF NOT EXISTS active_heat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_name TEXT,
                event_name TEXT,
                gender TEXT,
                heat TEXT,
                push_time TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        
        c.execute("DELETE FROM start_list")
        c.execute("DELETE FROM active_heat")
        c.execute("DELETE FROM system_settings WHERE key = 'active_track_heat'")

        # 🌟 2. 重新编排视为全新比赛：重置所有运动员历史成绩、名次、积分、检录以及接力棒次
        c.execute("""
            UPDATE registrations 
            SET score = '', 
                rank = '', 
                points = 0, 
                record_bonus = 0,
                lane = '',
                heat = '',
                relay_leg = ''  -- 🌟 彻底清空过往残留的接力棒次
        """)

        # 🌟 3. 删除之前生成的临时决赛记录（重新编排后需重新进行预赛并重新出线）
        c.execute("DELETE FROM registrations WHERE event_name LIKE '%(决赛)%' OR event_name LIKE '%（决赛）%'")

        # 🌟 4. 写入新的编排赛程
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
        import traceback; traceback.print_exc()
        return jsonify({"status": "error", "msg": "写入数据库失败: " + str(e)})
    finally:
        conn.close()
@app.route('/api/get_referee_meta')
def get_referee_meta():
    conn = get_db_connection()
    c = conn.cursor()
    
    # 1. 读取配置表大项类型
    event_type_rows = c.execute("SELECT name, type, is_fun FROM cfg_events").fetchall()
    event_type_map = {}
    for r in event_type_rows:
        e_type = r['type'] or '径赛'
        if str(r['is_fun']) == '1' or '趣味' in str(e_type):
            e_type = '趣味'
        event_type_map[r['name']] = e_type
        # 去掉各类括号保留纯核心名称
        pure_name = re.sub(r'[\(（].*?[\)）]', '', r['name']).strip()
        event_type_map[pure_name] = e_type

    # 2. 查询已有赛程或报名的项目
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
        
        # 🌟 关键：清洗掉所有的 (场地1)、(场地2)、(预赛)、(决赛) 后去匹配配置
        clean_core = re.sub(r'[\(（].*?[\)）]', '', e).strip()
        matched_type = event_type_map.get(e) or event_type_map.get(clean_core)
        
        # 如果还是没在配置表匹配到，田赛常见项目关键词保底兜底
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

        # 1. 检查是否为趣味项目
        cfg = c.execute("SELECT type, is_fun FROM cfg_events WHERE name = ?", (event_name,)).fetchone()
        is_fun = cfg and (cfg['type'] == '趣味' or cfg['type'] == '趣味项目' or str(cfg['is_fun']) == '1')

        # 2. 趣味项目处理逻辑（支持 group_name 为空时跨组全量展示）
        if is_fun:
            # 检查 start_list 里是否已有该项目的编排
            check_sql = "SELECT COUNT(*) FROM start_list WHERE event_name = ? AND (group_name = ? OR ? = '')"
            count_in_start = c.execute(check_sql, (event_name, group_name, group_name)).fetchone()[0]
            
            # 仅当 start_list 里完全没有编排时，才回退直接读报名表 registrations
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

        # 3. 判定当前查询是否为独立决赛
        is_finals = ('决赛' in event_name) and ('预赛' not in event_name)

        # 4. 常规径赛/田赛/已编排趣味项目的关联查询（严格匹配组别与性别）
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
                IFNULL(r.attempts_json, s.attempts_json) AS attempts_json -- 🌟 增加这行
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
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
def check_and_auto_publish_finals(g_name, event_name, gender):
    """
    后台全自动引擎：只要预赛有效完赛人数满足出线要求，自动秒级排定并发布决赛
    """
    clean_core = re.sub(r'\(.*?\)|（.*?）|预赛|决赛', '', event_name).strip()
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        # 1. 检查项目是否配置预赛
        cfg = c.execute("SELECT has_prelim, qualify_count FROM cfg_events WHERE name=? OR name LIKE ?", 
                        (clean_core, f"%{clean_core}%")).fetchone()
        if not cfg or str(cfg['has_prelim']) not in ['1', 'true', 'True']:
            return

        top_n = 8
        if cfg and 'qualify_count' in cfg.keys() and cfg['qualify_count']:
            try: top_n = int(cfg['qualify_count'])
            except: top_n = 8

        clean_gender = '女' if '女' in gender else ('男' if '男' in gender else gender)
        final_event_name = f"{clean_core} (决赛)"

        # 2. 提取预赛有效成绩（直接使用与成绩公告完全一致的数据源）
        query = """
            SELECT DISTINCT r.name, r.team_name, r.bib, r.team_id, 
                   IFNULL(r.score, s.score) as score
            FROM registrations r
            LEFT JOIN start_list s 
                ON s.name = r.name AND s.team_name = r.team_name
            WHERE (r.group_name = ? OR r.group_name LIKE ? || '%')
              AND (r.gender = ? OR r.gender LIKE ? || '%')
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
            g_name, g_name[:2],
            clean_gender, clean_gender,
            clean_core, f"{clean_core} (预赛)", clean_core, clean_core, clean_core
        )).fetchall()

        # 3. 过滤出有效完赛选手（秒数 > 0 且非弃权）
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

        # 只要没有有效成绩，不发布
        if not valid_list:
            return

        # 4. 按成绩升序排序，截取前 top_n 名
        valid_list.sort(key=lambda x: x['_val'])
        finalists = valid_list[:top_n]

        # 5. 标准道次规则 (4-5-3-6-2-7-1-8)
        lane_presets = {
            8: [4, 5, 3, 6, 2, 7, 1, 8],
            6: [3, 4, 2, 5, 1, 6],
            4: [2, 3, 1, 4]
        }
        lane_order = lane_presets.get(len(finalists), list(range(1, len(finalists) + 1)))

        # 6. 读取既定决赛赛程时间槽位
        dummy_meta = c.execute("""
            SELECT est_time, time_index, total_lanes, group_name 
            FROM start_list 
            WHERE (group_name = ? OR group_name LIKE ? || '%')
              AND (gender = ? OR gender LIKE ? || '%')
              AND (event_name LIKE ? || '%决赛%' OR event_name = ?)
            ORDER BY time_index DESC LIMIT 1
        """, (g_name, g_name[:2], clean_gender, clean_gender, clean_core, final_event_name)).fetchone()

        actual_g_name = dummy_meta['group_name'] if dummy_meta else g_name
        base_est = dummy_meta['est_time'] if (dummy_meta and dummy_meta['est_time']) else '第2天下午 14:30'
        base_tidx = dummy_meta['time_index'] if (dummy_meta and dummy_meta['time_index'] is not None) else 500
        total_lanes = dummy_meta['total_lanes'] if dummy_meta else 8

        g_info = c.execute("SELECT id FROM cfg_groups WHERE name=?", (actual_g_name,)).fetchone()
        gid = g_info['id'] if g_info else 0

        # 7. 清除旧决赛待定占位行
        c.execute("""
            DELETE FROM start_list 
            WHERE (group_name = ? OR group_name = ? OR group_name LIKE ? || '%') 
              AND (gender = ? OR gender LIKE ? || '%')
              AND (event_name LIKE ? || '%决赛%' OR event_name = ?)
        """, (g_name, actual_g_name, g_name[:2], clean_gender, clean_gender, clean_core, final_event_name))

        c.execute("""
            DELETE FROM registrations 
            WHERE (group_name = ? OR group_name = ? OR group_name LIKE ? || '%') 
              AND (gender = ? OR gender LIKE ? || '%')
              AND (event_name LIKE ? || '%决赛%' OR event_name = ?)
        """, (g_name, actual_g_name, g_name[:2], clean_gender, clean_gender, clean_core, final_event_name))

        # 8. 写入前 N 名晋级选手
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
        print(f"⚡ [后台全自动引擎] 【{actual_g_name} {clean_gender}·{clean_core}】已自动提取前 {len(finalists)} 名出线选手排定决赛道次！")
    except Exception as e:
        conn.rollback()
        import traceback; traceback.print_exc()
    finally:
        conn.close()
@app.route('/api/submit_score', methods=['POST'])
def submit_score():
    data = request.json or {}
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        raw_val = str(data.get('score', '')).strip()
        reg_id = data.get('id') or data.get('reg_id')
        
        # 🌟 安全提取 attempts 字段，如果没有传入则默认为空字符串
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

        # 1. 优先从 registrations 查询（🌟 补上 gender 字段，避免后续调用报错）
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

        # 格式化成绩
        if raw_val:
            is_field = False
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

        # 2. 检查表结构是否存在 attempts_json 字段
        try: c.execute("ALTER TABLE registrations ADD COLUMN attempts_json TEXT DEFAULT ''")
        except: pass
        try: c.execute("ALTER TABLE start_list ADD COLUMN attempts_json TEXT DEFAULT ''")
        except: pass

        # 3. 双向同步更新 registrations 和 start_list
        is_relay = re.search(r'4[xX*×]|接力', event_name) is not None
        if is_relay:
            c.execute("UPDATE registrations SET score = ?, attempts_json = ? WHERE team_name = ? AND event_name = ?", 
                      (formatted_score, attempts_json, team_name, event_name))
        else:
            c.execute("UPDATE registrations SET score = ?, attempts_json = ? WHERE id = ?", 
                      (formatted_score, attempts_json, target_reg_id))

        c.execute("""
            UPDATE start_list SET score = ?, attempts_json = ? 
            WHERE name = ? AND team_name = ? AND group_name = ? AND event_name = ?
        """, (formatted_score, attempts_json, name, team_name, group_name, event_name))
            
        conn.commit()
    except Exception as e:
        import traceback; traceback.print_exc()
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()

    # 🌟 核心：放在函数内部、连接安全关闭之后触发自动出线
    try:
        check_and_auto_publish_finals(group_name, event_name, gender)
    except Exception as e:
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
    """
    直接将出线名单秒级写入决赛道次与报名表
    """
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

        g_info = c.execute("SELECT id, name FROM cfg_groups WHERE name=? OR name LIKE ? || '%'", (g_name, g_name[:2])).fetchone()
        gid = g_info['id'] if g_info else 0
        actual_g_name = g_info['name'] if g_info else g_name

        dummy_meta = c.execute("""
            SELECT est_time, time_index, total_lanes 
            FROM start_list 
            WHERE (group_name = ? OR group_name = ? OR group_name LIKE ? || '%')
              AND (gender = ? OR gender LIKE ? || '%')
              AND (event_name LIKE ? || '%决赛%' OR event_name = ?)
            ORDER BY time_index DESC LIMIT 1
        """, (g_name, actual_g_name, g_name[:2], clean_gender, clean_gender, clean_core, final_event_name)).fetchone()

        base_est = dummy_meta['est_time'] if (dummy_meta and dummy_meta['est_time']) else '第2天下午 14:30'
        base_tidx = dummy_meta['time_index'] if (dummy_meta and dummy_meta['time_index'] is not None) else 500
        total_lanes = dummy_meta['total_lanes'] if dummy_meta else 8

        # 彻底清除原本的待定占位
        c.execute("""
            DELETE FROM start_list 
            WHERE (group_name = ? OR group_name = ? OR group_name LIKE ? || '%') 
              AND (gender = ? OR gender LIKE ? || '%')
              AND (event_name LIKE ? || '%决赛%' OR event_name = ?)
        """, (g_name, actual_g_name, g_name[:2], clean_gender, clean_gender, clean_core, final_event_name))

        c.execute("""
            DELETE FROM registrations 
            WHERE (group_name = ? OR group_name = ? OR group_name LIKE ? || '%') 
              AND (gender = ? OR gender LIKE ? || '%')
              AND (event_name LIKE ? || '%决赛%' OR event_name = ?)
        """, (g_name, actual_g_name, g_name[:2], clean_gender, clean_gender, clean_core, final_event_name))

        lane_presets = {
            8: [4, 5, 3, 6, 2, 7, 1, 8],
            6: [3, 4, 2, 5, 1, 6],
            4: [2, 3, 1, 4]
        }
        lane_order = lane_presets.get(len(qualifiers), list(range(1, len(qualifiers) + 1)))

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
        print(f"🎉 决赛名单已秒级发布入库：【{actual_g_name} {clean_gender}·{clean_core}】出线 {len(qualifiers)} 人！")
        return jsonify({"status": "success", "msg": f"🎉 成功发布 {len(qualifiers)} 人进入决赛！"})
    except Exception as e:
        conn.rollback()
        import traceback; traceback.print_exc()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
@app.route('/api/publish_finals', methods=['POST'])
def publish_finals():
    data = request.json or {}
    display_name = data.get('final_event_name') # 如: 100米 (决赛)
    g_name = data.get('group_name')             # 如: A组（初一 初二）
    gender = data.get('gender')                 # 如: 女
    athletes = data.get('athletes', [])

    if not athletes: 
        return jsonify({"status": "error", "msg": "决赛出线名单为空"})

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        
        # 1. 查找组别 ID
        g_info = c.execute("SELECT id FROM cfg_groups WHERE name=?", (g_name,)).fetchone()
        gid = g_info['id'] if g_info else 0

        # 2. 核心：在已编排的 start_list 中寻找该决赛的原生排程（保留其在“第2天 下午 14:30”的槽位）
        clean_core = re.sub(r'\(.*?\)|（.*?）|决赛|预赛', '', display_name).strip()
        existing_slots = c.execute("""
            SELECT id, est_time, time_index, total_lanes, type, is_field, heat, lane
            FROM start_list 
            WHERE group_name = ? AND gender = ? 
              AND (event_name = ? OR event_name = ? OR event_name LIKE ? || '%决赛%')
            ORDER BY CAST(heat AS INTEGER) ASC, CAST(lane AS INTEGER) ASC
        """, (g_name, gender, display_name, f"{clean_core} (决赛)", clean_core)).fetchall()

        # 3. 清理已有的决赛成绩与报名绑定（重置该决赛人员名单）
        c.execute("DELETE FROM registrations WHERE group_name=? AND event_name=? AND gender=?", (g_name, display_name, gender))

        # 4. 如果原本智能编排中已经排好了决赛道次（优先注入既定道次）
        if existing_slots and len(existing_slots) > 0:
            # 记录基础赛程信息
            base_est = existing_slots[0]['est_time'] or '第2天下午 14:30'
            base_tidx = existing_slots[0]['time_index'] or 100
            
            # 删除旧槽位重新规整写入真实的晋级选手
            c.execute("DELETE FROM start_list WHERE group_name=? AND gender=? AND (event_name=? OR event_name=? OR event_name LIKE ? || '%决赛%')", 
                      (g_name, gender, display_name, f"{clean_core} (决赛)", clean_core))

            for i, ath in enumerate(athletes):
                lane = str(ath.get('finalLane', i + 1))
                heat = '1' # 决赛通常为第1组（或依据原编排）
                
                # 写入报名表
                c.execute("""INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, score)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')""", 
                          (gid, g_name, ath.get('team_id', 0), ath.get('team_name', ''), ath.get('name', ''), gender, ath.get('bib', ''), display_name))
                
                # 写入编排表（完整继承编排好的单元、具体时间和序号）
                c.execute("""INSERT INTO start_list (group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field, score, checked_in, is_started)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sprint', 8, ?, ?, 0, '', 0, 0)""",
                          (g_name, display_name, gender, heat, lane, ath.get('bib', ''), ath.get('name', ''), ath.get('team_name', ''), base_est, base_tidx))
        else:
            # 如果原来完全没排过此决赛，从预赛顺延并继承时间
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
        import traceback; traceback.print_exc()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
@app.route('/api/manage_team_passwords', methods=['POST'])
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
        except Exception as e:
            print(f"生成错误: {e}")

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

    # 🌟 1. 坚决拦截从决赛生成决赛
    if '决赛' in base_evt:
        return jsonify({"status": "error", "msg": f"【{base_evt}】当前已是决赛，无法从决赛中再次提取决赛名单！"})

    conn = get_db_connection()
    c = conn.cursor()
    try:
        # 清洗项目核心名
        clean_core = re.sub(r"男子|女子|混合", "", base_evt).strip()
        clean_core = clean_core.replace(' (预赛)', '').replace(' (决赛)', '').replace('()', '').replace('（）', '').strip()

        # 2. 判定该项目是否属于有预赛的项目
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
        
        # 🌟 核心过滤：只保留转换为秒数大于 0 的有效成绩选手
        athletes = [dict(r) for r in rows if parse_time_to_seconds(r['score']) > 0]
        
        if not athletes:
            return jsonify({"status": "error", "msg": f"未找到【{g_name} {gender}·{clean_core}】的有效完赛成绩，请确认是否所有选手都未完赛或已弃权！"})

        is_field = clean_core.endswith('跳远') or clean_core.endswith('跳高') or clean_core.endswith('铅球') or clean_core.endswith('实心球') or clean_core.endswith('标枪')
        
        # 正式排序（径赛从小到大，弃权已被剔除，不会干扰第1名）
        athletes.sort(key=lambda x: parse_time_to_seconds(x['score']), reverse=is_field)
        # 🌟 4. 规范命名，避免多重嵌套括号
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
# ============================================================
# 📥 导入导出接口
# ============================================================
@app.route('/api/export_system')
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
        # 修复占位符冒号
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
        import traceback; traceback.print_exc()
        return jsonify({"status": "error", "msg": "导入失败: " + str(e)})
    finally:
        if 'conn' in locals(): conn.close()

# ============================================================
# 🛠️ 数据库初始化与字段对齐补丁
# ============================================================
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
        except: pass
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
    
    for col, t in [("total_lanes", "INTEGER DEFAULT 8"), ("est_time", "TEXT DEFAULT ''"), ("time_index", "INTEGER DEFAULT 0"), ("is_field", "INTEGER DEFAULT 0"), ("score", "TEXT DEFAULT ''"), ("checked_in", "INTEGER DEFAULT 0")]:
        try: c.execute(f"ALTER TABLE start_list ADD COLUMN {col} {t}")
        except: pass

    try: c.execute("ALTER TABLE registrations ADD COLUMN attempts_json TEXT DEFAULT ''")
    except: pass
    try: c.execute("ALTER TABLE start_list ADD COLUMN attempts_json TEXT DEFAULT ''")
    except: pass   
    try: c.execute("ALTER TABLE cfg_events ADD COLUMN qualify_count INTEGER DEFAULT 8")
    except: pass
    # 🌟 移入函数内部：在 conn.close() 之前执行数据自动校准
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
    except Exception as e:
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
        # 查询所有已录入有效成绩的记录
        sql = """
            SELECT group_name, team_name, gender, event_name, score
            FROM registrations
            WHERE score IS NOT NULL AND score != ''
        """
        rows = c.execute(sql).fetchall()
        
        # 预先读取项目配置（判断田径属性）
        cfgs = {r['name']: dict(r) for r in c.execute("SELECT name, type FROM cfg_events").fetchall()}

        grade_analysis = {}

        for r in rows:
            g_name = r['group_name'] or '未知年级'
            t_name = r['team_name'] or '未知班级'
            gen = r['gender']
            raw_evt = r['event_name']
            
            # 清洗项目名称（去除预赛、决赛、组别、场地等后缀，保留纯项目名）
            core_evt = re.sub(r"\(.*?\)|（.*?）|决赛|预赛|男子|女子|混合|第\d+组|场地\d+", "", raw_evt).strip()
            if not core_evt:
                continue

            sec_val = parse_time_to_seconds(r['score'])
            if sec_val <= 0:
                continue

            # 严格判断是否为田赛（大值优先）
            is_field = False
            cfg = cfgs.get(core_evt)
            if cfg and (cfg.get('type') == '田赛' or '田' in str(cfg.get('type'))):
                is_field = True
            elif any(k in core_evt for k in ['跳', '投', '铅球', '实心球', '标枪', '铁饼', '引体', '仰卧']):
                is_field = True

            if g_name not in grade_analysis:
                grade_analysis[g_name] = {"events": {}, "teams": {}}

            # 1. 单项成绩归纳（100米、200米等各自分立，绝不合并）
            if core_evt not in grade_analysis[g_name]["events"]:
                grade_analysis[g_name]["events"][core_evt] = {
                    "男": [],
                    "女": [],
                    "is_field": is_field
                }
            if gen in grade_analysis[g_name]["events"][core_evt]:
                grade_analysis[g_name]["events"][core_evt][gen].append(sec_val)

            # 2. 班级横向对比归纳
            if t_name not in grade_analysis[g_name]["teams"]:
                grade_analysis[g_name]["teams"][t_name] = {}
            if core_evt not in grade_analysis[g_name]["teams"][t_name]:
                grade_analysis[g_name]["teams"][t_name][core_evt] = {"男": [], "女": []}
            if gen in grade_analysis[g_name]["teams"][t_name][core_evt]:
                grade_analysis[g_name]["teams"][t_name][core_evt][gen].append(sec_val)

        # 结构化格式化输出
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
        import traceback; traceback.print_exc()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
@app.route('/api/batch_save_events', methods=['POST'])
def batch_save_events():
    data = request.get_json() or {}
    events = data.get('events', [])
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        for col, dtype in [
            ("is_fun", "TEXT DEFAULT '0'"), ("allowed_groups", "TEXT DEFAULT ''"), 
            ("duration", "REAL DEFAULT 5"), ("venue_count", "INTEGER DEFAULT 1"),
            ("qualify_count", "INTEGER DEFAULT 8") # 🌟 补齐字段
        ]:
            try: c.execute(f"ALTER TABLE cfg_events ADD COLUMN {col} {dtype}")
            except: pass
        
        c.execute("DELETE FROM cfg_events")
        
        for i, e in enumerate(events):
            has_prelim_str = '1' if e.get('hasPrelim') else '0'
            is_relay_str = '1' if e.get('isRelay') else '0'
            need_lane_str = '1' if e.get('needLane') else '0'
            is_fun_str = '1' if e.get('isFun', False) else '0'
            
            limit_val = e.get('limit') if e.get('limit') is not None else 8
            dur_val = e.get('duration') if e.get('duration') is not None else 5
            ven_val = e.get('venueCount') if e.get('venueCount') is not None else 1
            qualify_val = int(e.get('qualifyCount') or 8) # 🌟 提取录取名次
            
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
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "message": str(ex)})
    finally:
        conn.close()
# 统一在主逻辑初始化数据库
init_db()
force_sync_and_upgrade_db()
upgrade_records()

if __name__ == '__main__':
    local_ip = get_host_ip()
    print(f"✅ 启动成功！")
    print(f"👉 领队端: http://{local_ip}:5000/bm")
    print(f"👉 管理端: http://{local_ip}:5000/admin/login")
    print(f"👉 裁判端: http://{local_ip}:5000/referee/login")
    
    app.jinja_env.auto_reload = True
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    serve(app, host='0.0.0.0', port=5000, threads=16)