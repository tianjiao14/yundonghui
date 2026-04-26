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

app = Flask(__name__)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "sports.db")

app.secret_key = 'sports_day_secret_key_2026' # 🔐 密钥

def to_bool_str(val):
    """将各种类型的布尔值统一转换为字符串 '1' 或 '0'"""
    if val is None:
        return '0'
    s = str(val).lower()
    return '1' if s in ['true', '1', 'yes', 'on'] else '0'

DB_FILE = os.path.join(BASE_DIR, "data", "sports_data.db")
ADMIN_PASSWORD = "admin888"
REFEREE_PASSWORD = "ref888"

import re

@app.route('/api/recalculate_all_points', methods=['POST'])
def recalculate_all_points():
    conn = get_db_connection()
    c = conn.cursor()
    count = 0
    try:
        # 1. 初始化：清空所有积分
        c.execute("UPDATE registrations SET points = 0")
        
        groups_genders = c.execute("SELECT DISTINCT group_name, gender FROM registrations WHERE group_name != ''").fetchall()
        all_cfgs = {row['name']: dict(row) for row in c.execute("SELECT * FROM cfg_events").fetchall()}

        for gg in groups_genders:
            g_name, gender = gg['group_name'], gg['gender']
            rows = c.execute("SELECT DISTINCT event_name FROM registrations WHERE group_name = ? AND gender = ? AND score != ''", (g_name, gender)).fetchall()
            distinct_events = [r['event_name'] for r in rows]
            if not distinct_events: continue

            event_map = {}
            for evt in distinct_events:
                # 提取核心名，用于匹配配置（保留初中/高中等前缀以便区分纪录）
                core = re.sub(r"\(.*?\)|（.*?）|决赛|预赛|及格赛|男子|女子|混合|男|女|第一组|第二组|第三组|第四组|第\d+组", "", evt).strip()
                if core not in event_map: event_map[core] = []
                event_map[core].append(evt)

            for core_name, sub_events in event_map.items():
                # 🚀 修正：智能匹配“男/女”与“男子/女子”
                cfg = all_cfgs.get(core_name)
                if not cfg:
                    # 尝试补齐“男子/女子”前缀再找
                    prefix = "女子" if gender == "女" else "男子"
                    cfg = all_cfgs.get(prefix + core_name)
                
                if not cfg: # 模糊匹配兜底
                    for k, v in all_cfgs.items():
                        if k in core_name or core_name in k: cfg = v; break
                
                # B. 🚀 关键逻辑：判定是否为预决赛制
                # 读取你在 Step 1 勾选的“预决”开关
                is_prelim_cfg = False
                if cfg:
                    has_pre_val = cfg.get('has_prelim') or cfg.get('hasPrelim')
                    is_prelim_cfg = (to_bool_str(has_pre_val) == '1')

                # C. 确定计算范围
                if is_prelim_cfg:
                    # 如果是预决制项目，【只取】包含“决赛”字样的成绩
                    target_events = [e for e in sub_events if '决赛' in e]
                else:
                    # 如果是直接决赛项目（如1500米、跳远），取该核心名下的所有成绩
                    target_events = sub_events
                
                # 如果该项目该组别目前还没有决赛成绩，则跳过，该项积分为 0
                if not target_events:
                    continue

                # D. 提取数据（后面逻辑保持您要求的破纪录、并列、双倍等功能）
                is_field = False
                field_keywords = ['跳', '投', '掷', '铅球', '实心球', '标枪', '铁饼', '球', '引体', '仰卧']
                if cfg and (cfg.get('type') == '田赛' or '田' in str(cfg.get('type'))): is_field = True
                elif any(kwd in core_name for kwd in field_keywords): is_field = True
                
                record_bonus = 0
                try: record_bonus = int(cfg.get('record_bonus') or cfg.get('recordBonus') or 0) if cfg else 0
                except: record_bonus = 0
                
                event_record = cfg.get('record') if cfg else None
                rec_val = parse_time_to_seconds(event_record) if (event_record and str(event_record).strip()) else None

                placeholders = ','.join(['?'] * len(target_events))
                sql = f"SELECT id, name, team_name, event_name, score FROM registrations WHERE group_name=? AND gender=? AND event_name IN ({placeholders}) AND score != ''"
                data_rows = c.execute(sql, [g_name, gender] + target_events).fetchall()
                if not data_rows: continue
                
                # 接力/个人去重
                unique_entries = {} 
                for item in [dict(r) for r in data_rows]:
                    is_relay_event = re.search(r'4[xX*]|接力', item['event_name']) is not None
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

                # 并列名次计分
                current_rank = 1
                for i, item in enumerate(final_list):
                    if i > 0 and item['_val'] != final_list[i-1]['_val']:
                        current_rank = i + 1
                    
                    p = 0
                    if current_rank <= len(rules):
                        p = rules[current_rank - 1]
                        if is_double: p *= 2
                    
                    # 破纪录判断
                    if rec_val is not None and rec_val > 0 and item['_val'] > 0:
                        is_broken = (item['_val'] > rec_val) if is_field else (item['_val'] < rec_val)
                        if is_broken: p += record_bonus

                    if p > 0:
                        c.execute("UPDATE registrations SET points = ? WHERE id = ?", (p, item['id']))
                count += 1
        
        conn.commit()
        return jsonify({'status': 'success', 'msg': f'计算完毕！已处理 {count} 个项目。预决赛逻辑已校准。'})
    except Exception as e:
        import traceback; traceback.print_exc()
        conn.rollback()
        return jsonify({'status': 'error', 'msg': str(e)})
    finally:
        conn.close()
@app.route('/api/update_point', methods=['POST'])
def update_point():
    data = request.json
    try:
        conn = get_db_connection()
        conn.execute("UPDATE registrations SET points = ? WHERE id = ?", (data['points'], data['id']))
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'msg': '积分修改成功'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})
# 1. 团体总分：严格遍历成绩公告表 
@app.route('/api/calculate_team_ranking', methods=['POST'])
def calculate_team_ranking():
    g_name = request.json.get('group_name')
    conn = get_db_connection()
    c = conn.cursor()
 
    sql = """
        SELECT 
            team_name as name, 
            SUM(points) as score,
            SUM(CASE WHEN points >= 9 THEN 1 ELSE 0 END) as gold,
            SUM(CASE WHEN points = 7 THEN 1 ELSE 0 END) as silver,
            SUM(CASE WHEN points = 6 THEN 1 ELSE 0 END) as bronze
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
         
            # 提取核心项目名（如 男子100米决赛 -> 100米）
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
def parse_time_to_seconds(val):
    if not val or str(val).strip() == "": return 0.0 
    try:
        s = str(val).strip().replace('：', ':').replace('。', '.')
     
        if ':' in s:
            parts = s.split(':')
            if len(parts) == 2: return int(parts[0]) * 60 + float(parts[1]) # 分:秒
            elif len(parts) == 3: return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
   
        return float(s)
    except:
        return 0.0


def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS registrations
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  group_id INTEGER, group_name TEXT, team_id INTEGER, team_name TEXT,
                  name TEXT, gender TEXT, bib TEXT, event_name TEXT,  
                  score TEXT DEFAULT '', rank TEXT DEFAULT '', lane TEXT DEFAULT '', 
                  heat TEXT DEFAULT '', submit_time TEXT)''')

    c.execute('''CREATE TABLE IF NOT EXISTS cfg_groups (id INTEGER PRIMARY KEY, name TEXT, prefix TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS cfg_teams (id INTEGER PRIMARY KEY, group_id INTEGER, name TEXT, leader TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS cfg_events 
                 (id INTEGER PRIMARY KEY, name TEXT, type TEXT, gender TEXT, score_rule TEXT, record TEXT, record_bonus TEXT, is_double_score BOOLEAN, need_lane BOOLEAN, has_prelim BOOLEAN, is_relay BOOLEAN, limit_count INTEGER, allowed_groups TEXT DEFAULT '')''')
    c.execute('''CREATE TABLE IF NOT EXISTS sys_config (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS team_auth (id INTEGER PRIMARY KEY AUTOINCREMENT, team_name TEXT, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS start_list 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  group_name TEXT, 
                  event_name TEXT, 
                  gender TEXT, 
                  heat TEXT, 
                  lane TEXT, 
                  bib TEXT, 
                  name TEXT, 
                  team_name TEXT, 
                  type TEXT)''')
    c.execute("CREATE INDEX IF NOT EXISTS idx_reg_team ON registrations(team_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_reg_event ON registrations(event_name)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_start_list_event ON start_list(event_name)")
    conn.commit()
    conn.close()

# ============================================================
# 🔒 独立权限拦截器
# ============================================================
def login_required(role_needed):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_role' not in session:
                if role_needed == 'admin': 
                    return redirect('/admin/login') # 必须与下方路由一致
                elif role_needed == 'referee': 
                    return redirect('/referee/login')
                else: 
                    return redirect('/team') # 领队去 /team
            
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
def get_db_connection():
    conn = sqlite3.connect(DB_FILE, timeout=20) 
    conn.execute('PRAGMA journal_mode=WAL;') 
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/team')
def team_login(): 
    return render_template('team_login.html')

@app.route('/')
@app.route('/admin')
@login_required('admin')
def admin():

    local_ip = get_host_ip()
  
    return render_template('admin.html', local_ip=local_ip)

@app.route('/index')
@login_required('team')  
def index():
    conn = get_db_connection()
    c = conn.cursor()

    row = c.execute("SELECT value FROM sys_config WHERE key='title'").fetchone()
    page_title = row['value'] if row else "运动会系统"
    user_role = session.get('user_role')
    group_id = session.get('group_id')
    team_id = session.get('team_id')

    if user_role == 'admin':
        # 管理员：可以看到所有组别和班级
        groups = [dict(r) for r in c.execute("SELECT * FROM cfg_groups").fetchall()]
        teams = [dict(r) for r in c.execute("SELECT * FROM cfg_teams").fetchall()]
    else:
        # 领队：只下发自己所属的组别和班级数据
        groups = [dict(r) for r in c.execute("SELECT * FROM cfg_groups WHERE id=?", (group_id,)).fetchall()]
        teams = [dict(r) for r in c.execute("SELECT * FROM cfg_teams WHERE id=?", (team_id,)).fetchall()]

    events = [dict(r) for r in c.execute("SELECT * FROM cfg_events").fetchall()]
    conn.close()

    return render_template('index.html', 
                         user_group_id=group_id, 
                         user_team_id=team_id,
                         title=page_title, 
                         groups=groups, 
                         teams_json=json.dumps(teams), 
                         events_json=json.dumps(events), 
                         user_role=user_role, 
                         team_name=session.get('team_name'))
@app.route('/admin/login')
def admin_login(): 
    return render_template('admin_login.html')


@app.route('/referee/login')
def referee_login(): return render_template('referee_login.html')

@app.route('/referee')
@login_required('referee')
def referee():
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; c = conn.cursor()
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
    data = request.json
    role_type = data.get('type')
    
    if role_type == 'admin':
        if data.get('password') == ADMIN_PASSWORD:
            session['user_role'] = 'admin'
            return jsonify({'status': 'success', 'redirect': '/admin'})
    elif role_type == 'referee':
        if data.get('password') == REFEREE_PASSWORD:
            session['user_role'] = 'referee'
            return jsonify({'status': 'success', 'redirect': '/referee'})
    elif role_type == 'team':
        username = data.get('username')
        password = data.get('password')
        conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; c = conn.cursor()
        auth_row = c.execute("SELECT password FROM team_auth WHERE team_name = ?", (username,)).fetchone()
        if not auth_row or str(auth_row['password']) != str(password):
            conn.close()
            return jsonify({'status': 'fail', 'msg': '认证失败：密码错误或账号不存在'})
        team_row = c.execute("SELECT id, group_id, name FROM cfg_teams WHERE name = ?", (username,)).fetchone()
        conn.close()
        if not team_row:
            return jsonify({'status': 'fail', 'msg': '认证失败：该代表队未配置'})
        session['user_role'] = 'team'  # 补充：必须设置user_role，否则权限拦截器会拦截
        session['team_id'] = team_row['id']      
        session['group_id'] = team_row['group_id']
        session['team_name'] = team_row['name']
        return jsonify({'status': 'success', 'redirect': '/'})
    return jsonify({'status': 'fail', 'msg': '认证失败：密码错误或账号不存在'})

@app.route('/api/logout')
def logout():
    role = session.get('user_role')
    session.clear()  # 清除所有会话数据
    if role == 'admin':
        return redirect('/admin/login')
    elif role == 'referee':
        return redirect('/referee/login')
    else:  # 领队角色或未识别角色，跳转到相对路径的登录页
        return redirect('/team')
# ============================================================
# ⚙️ 业务功能 API
# ============================================================
@app.route('/api/reset_system', methods=['POST'])
def reset_system():
    if session.get('user_role') != 'admin':
        return jsonify({"status": "error", "msg": "无权操作"})
    
    mode = request.json.get('mode') # 'all' 或 'data_only'
    conn = get_db_connection()
    c = conn.cursor()
    
    try:
        c.execute("DELETE FROM registrations")
        c.execute("DELETE FROM start_list")
        c.execute("DELETE FROM team_auth")
 
        if mode == 'all':
            c.execute("DELETE FROM cfg_groups")
            c.execute("DELETE FROM cfg_teams")
            c.execute("DELETE FROM cfg_events")
            c.execute("DELETE FROM sqlite_sequence") # 重置自增 ID
            
        conn.commit()
        return jsonify({"status": "success", "msg": "系统已按要求重置"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
@app.route('/api/save_arrangement', methods=['POST'])
def save_arrangement():
    data = request.json
    group_name = data.get('group_name')
    event_name = data.get('event_name')
    arrangement = data.get('arrangement') 

    conn = get_db_connection()
    c = conn.cursor()
    try:

        c.execute("DELETE FROM start_list WHERE group_name=? AND event_name=?", (group_name, event_name))
        
        for item in arrangement:
            c.execute('''INSERT INTO start_list 
                (group_name, event_name, gender, heat, lane, bib, name, team_name, type) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (group_name, event_name, item['gender'], item['heat'], item['lane'], 
                 item['bib'], item['name'], item['team_name'], item.get('type', 'sprint')))
        
        conn.commit()
        return jsonify({"status": "success", "msg": "编排已保存到数据库"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()

@app.route('/api/get_arrangement', methods=['POST'])
def get_arrangement():
    data = request.json
    g_name = data.get('group_name')
    e_name = data.get('event_name')
    
    conn = get_db_connection()
    if g_name and e_name:
        rows = conn.execute("SELECT * FROM start_list WHERE group_name=? AND event_name=?", (g_name, e_name)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM start_list").fetchall()
    conn.close()
    
    return jsonify([dict(r) for r in rows])
@app.route('/api/export_teams')
def export_teams():
    conn = get_db_connection()
    c = conn.cursor()
    # 关联组别表获取组别名称
    query = """
        SELECT g.name as g_name, t.name as t_name, t.leader 
        FROM cfg_teams t
        JOIN cfg_groups g ON t.group_id = g.id
    """
    rows = c.execute(query).fetchall()
    conn.close()

    output = StringIO()
    output.write('\ufeff') # 防止 Excel 打开乱码
    writer = csv.writer(output)
    writer.writerow(['组别', '队名', '领队']) # 表头
    
    for r in rows:
        writer.writerow([r['g_name'], r['t_name'], r['leader'] or ''])
        
    mem = BytesIO()
    mem.write(output.getvalue().encode('utf-8-sig'))
    mem.seek(0)
    return send_file(mem, mimetype='text/csv', as_attachment=True, download_name=f'代表队名单_{datetime.now().strftime("%Y%m%d")}.csv')
@app.route('/api/import_teams', methods=['POST'])
def import_teams():
    if 'file' not in request.files: return jsonify({"status": "error", "msg": "未上传文件"})
    file = request.files['file']
    
    try:
        stream = StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
        csv_input = csv.reader(stream)
        next(csv_input) # 跳过表头
        
        conn = get_db_connection()
        c = conn.cursor()
        groups_map = {row['name']: row['id'] for row in c.execute("SELECT id, name FROM cfg_groups").fetchall()}
        
        success_count = 0
        for row in csv_input:
            if len(row) < 2: continue
            g_name, t_name = row[0].strip(), row[1].strip()
            leader = row[2].strip() if len(row) > 2 else ""
            
            gid = groups_map.get(g_name)
            if not gid: continue # 如果组别不存在则跳过
            c.execute("INSERT OR REPLACE INTO cfg_teams (group_id, name, leader) VALUES (?, ?, ?)", 
                      (gid, t_name, leader))
            success_count += 1
            
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "msg": f"✅ 成功导入 {success_count} 个代表队！"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})
@app.route('/api/events')
@login_required('team')
def get_events():
    team_id = session.get('team_id')
    conn = get_db_connection()
    c = conn.cursor()

    row = c.execute("SELECT value FROM sys_config WHERE key='maxPerEvent'").fetchone()
    MAX_PER_EVENT = int(row[0]) if row else 3
    events = c.execute("SELECT name, type, gender, allowed_groups FROM cfg_events").fetchall()
    # 结构：{ "100米": 2, "跳远": 1 }
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
            "rem": rem_text,   # 余额显示文字
            "is_full": is_full # 是否已满
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
        "total_athletes": total_athletes,      # 运动员总数
        "total_participations": total_participations # 报名总人次
    })
import socket

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
@app.route('/api/get_data')
def get_data_admin():
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; c = conn.cursor()
    db_groups = [dict(r) for r in c.execute("SELECT * FROM cfg_groups").fetchall()]
    db_teams = [dict(r) for r in c.execute("SELECT * FROM cfg_teams").fetchall()]
    for t in db_teams: t['groupId'] = t['group_id']
    db_events = [dict(r) for r in c.execute("SELECT * FROM cfg_events").fetchall()]
    
    # --- Start List ---
    db_schedule = []
    try:
        raw_sch = c.execute("SELECT * FROM start_list").fetchall()
        for r in raw_sch:
            item = dict(r)
            item['groupName'] = r['group_name']
            item['eventName'] = r['event_name']
            item['teamName'] = r['team_name']
            db_schedule.append(item)
    except: pass
    
    raw_regs = c.execute("SELECT * FROM registrations").fetchall()
    athletes_map = {}
    for r in raw_regs:
        key = f"{r['team_id']}_{r['name']}"
        if key not in athletes_map:
            athletes_map[key] = { "id": r['id'], "teamId": int(r['team_id']) if r['team_id'] else 0, "name": r['name'], "gender": r['gender'], "bib": r['bib'] or "", "events": [] }
        athletes_map[key]["events"].append(r['event_name'])
    config = {r['key']: r['value'] for r in c.execute("SELECT * FROM sys_config").fetchall()}
    conn.close()
    return jsonify({"groups": db_groups, "teams": db_teams, "events": db_events, "athletes": list(athletes_map.values()), "config": config, "schedule": db_schedule})


@app.route('/api/save_config', methods=['POST'])
def save_config():
    data = request.json
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        if 'groups' in data:
            c.execute("DELETE FROM cfg_groups")
            for g in data['groups']:
                c.execute("INSERT OR REPLACE INTO cfg_groups (id, name, prefix) VALUES (?, ?, ?)", (int(g['id']), g['name'], g['prefix']))
        if 'teams' in data:
            c.execute("DELETE FROM cfg_teams")
            for t in data['teams']:
                c.execute("INSERT OR REPLACE INTO cfg_teams (id, group_id, name, leader) VALUES (?, ?, ?, ?)", (int(t['id']), int(t['groupId']), t['name'], t.get('leader','')))
        if 'events' in data:
            c.execute("DELETE FROM cfg_events")
            for e in data['events']: 
                rule = e.get('scoreRule') or e.get('score_rule') or '9,7,6,5,4,3,2,1'
                rec = e.get('record') or ''
                bonus = e.get('recordBonus') or e.get('record_bonus') or 0
                sql = '''INSERT OR REPLACE INTO cfg_events 
                    (id, name, type, gender, score_rule, record, record_bonus, 
                     is_double_score, need_lane, has_prelim, is_relay, limit_count, allowed_groups) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''
                params = (
                    int(e['id']), 
                    e['name'], 
                    e['type'], 
                    e['gender'], 
                    str(rule), 
                    str(rec), 
                    str(bonus), 
                    to_bool_str(e.get('isDoubleScore') or e.get('is_double_score')), 
                    to_bool_str(e.get('needLane') or e.get('need_lane')), 
                    to_bool_str(e.get('hasPrelim') or e.get('has_prelim')), 
                    to_bool_str(e.get('isRelay') or e.get('is_relay')), 
                    int(e.get('limit', 2)),
                    str(e.get('allowedGroups', ''))  # 第 13 个字段：限定组别
                )
                
        
                c.execute(sql, params)
        if 'config' in data:
            for k, v in data['config'].items(): 
                c.execute("REPLACE INTO sys_config (key, value) VALUES (?, ?)", (k, str(v)))
        conn.commit()
        return jsonify({"status": "success", "msg": "✅ 配置已成功保存！"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": "保存失败: " + str(e)})
    finally:
        conn.close()

# ✅ 补充：领队端查询本班名单接口
@app.route('/api/team_members/<int:team_id>')
def get_team_members(team_id):
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; c = conn.cursor()
    rows = c.execute("SELECT id, name, gender, event_name FROM registrations WHERE team_id = ?", (team_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/add_athlete', methods=['POST'])
@login_required('team')
def add_athlete():
    data = request.json
    if session['user_role'] == 'team' and int(data.get('team_id')) != session.get('team_id'):
        return jsonify({"status": "error", "msg": "越权操作"}), 403
    conn = get_db_connection()
    c = conn.cursor()
    
    try:

        c.execute("BEGIN IMMEDIATE") 
        def get_cfg_val(key, default):
            row = c.execute("SELECT value FROM sys_config WHERE key=?", (key,)).fetchone()
            return int(row[0]) if row else default
        
        MAX_PER_PERSON = get_cfg_val('maxPerPerson', 2)
        MAX_PER_EVENT = get_cfg_val('maxPerEvent', 3)
        MAX_TOTAL = get_cfg_val('maxTotal', 20)
        
        team_id = data.get('team_id')
        name = data.get('name', '').strip()
        c.execute("DELETE FROM registrations WHERE team_id=? AND name=?", (team_id, name))
        selected_events = data.get('events', [])
        current_team_count = c.execute("SELECT COUNT(DISTINCT name) FROM registrations WHERE team_id=?", (team_id,)).fetchone()[0]
        exists = c.execute("SELECT 1 FROM registrations WHERE team_id=? AND name=?", (team_id, name)).fetchone()
        
        if not exists and current_team_count >= MAX_TOTAL:
            return jsonify({"status": "error", "msg": f"班级报名已达上限（{MAX_TOTAL}人）"})
       # --- 核心修复：多维度动态限额逻辑 ---
        for evt in selected_events:
            # 获取项目类型、是否接力、以及项目性别
            evt_info = c.execute("SELECT type, is_relay, gender, limit_count FROM cfg_events WHERE name=?", (evt,)).fetchone()
            
            # 1. 趣味项目：不限额
            if evt_info and (evt_info['type'] == '趣味' or '趣味' in str(evt_info['type'])):
                continue
            
            # 2. 确定限额逻辑
            is_relay = to_bool_str(evt_info['is_relay']) == '1' if evt_info else False
            gender_attr = evt_info['gender'] if evt_info else ""
            
            if gender_attr == '混合' and is_relay:
                # 🏆 混合接力：适配 5男5女 或 4x100混合等，设为 10 人
                current_limit = 10 
            elif is_relay:
                # 普通接力：4 人
                current_limit = 4 
            else:
                # 普通个人项目：使用全局设置（默认3）
                current_limit = MAX_PER_EVENT 
            
            count_in_evt = c.execute("SELECT COUNT(*) FROM registrations WHERE team_id=? AND event_name=?", (team_id, evt)).fetchone()[0]
            
            if count_in_evt >= current_limit:
                return jsonify({"status": "error", "msg": f"项目【{evt}】名额（每班限报{current_limit}人）已被抢占"})
        g_info = c.execute("SELECT name FROM cfg_groups WHERE id=?", (data['group_id'],)).fetchone()
        t_info = c.execute("SELECT name FROM cfg_teams WHERE id=?", (team_id,)).fetchone()
        submit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for evt in selected_events:
            c.execute("""INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, event_name, submit_time) 
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", 
                      (data['group_id'], g_info[0], team_id, t_info[0], name, data.get('gender'), evt, submit_time))
        
        conn.commit() # 提交事务
        return jsonify({"status": "success", "msg": "报名成功！"})
        
    except Exception as e:
        conn.rollback() # 出错回滚
        return jsonify({"status": "error", "msg": f"系统繁忙: {str(e)}"})
    finally:
        conn.close()

@app.route('/api/delete_athlete', methods=['POST'])
def delete_athlete():
    data = request.json; conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    try:
        if 'name' in data and 'team_id' in data: c.execute("DELETE FROM registrations WHERE team_id=? AND name=?", (data['team_id'], data['name']))
        else: c.execute("DELETE FROM registrations WHERE id = ?", (data['id'],))
        conn.commit(); return jsonify({"status": "success"})
    finally: conn.close()

# ✅ 补充：发布编排结果给裁判
@app.route('/api/save_schedule_to_db', methods=['POST'])
def save_schedule_to_db():
    schedule_data = request.json
    if not schedule_data: return jsonify({"status": "error", "msg": "没有接收到编排数据"})
    try:
        conn = sqlite3.connect(DB_FILE); c = conn.cursor()
        c.execute("DELETE FROM start_list")
        for item in schedule_data:
            c.execute('''INSERT INTO start_list (group_name, event_name, gender, heat, lane, bib, name, team_name, type) 
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
                      (item.get('groupName') or item.get('group_name'), 
                       item.get('eventName') or item.get('event_name'), 
                       item.get('gender'), item.get('heat'), item.get('lane'), 
                       item.get('bib'), item.get('name'), 
                       item.get('teamName') or item.get('team_name'), 
                       item.get('type')))
        conn.commit(); conn.close()
        return jsonify({"status": "success", "msg": "发布成功"})
    except Exception as e: return jsonify({"status": "error", "msg": str(e)})


    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM start_list")
        
        # 插入新数据
        for item in schedule_data:
            c.execute('''
                INSERT INTO start_list 
                (group_name, event_name, gender, heat, lane, bib, name, team_name, type) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                item.get('group_name'), item.get('event_name'), item.get('gender'),
                item.get('heat'), item.get('lane'), item.get('bib'),
                item.get('name'), item.get('team_name'), item.get('type')
            ))
            
        conn.commit()
        conn.close()
        return jsonify({"status": "success", "msg": "发布成功"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route('/api/get_referee_meta')
def get_referee_meta():
    conn = get_db_connection()
    c = conn.cursor()
    rows = c.execute("""
        SELECT DISTINCT group_name, gender, event_name 
        FROM start_list 
        ORDER BY group_name, gender, event_name
    """).fetchall()
    conn.close()
    
    data = {}
    for r in rows:
        g, gen, e = r['group_name'], r['gender'], r['event_name']
        if g not in data: data[g] = {}
        if gen not in data[g]: data[g][gen] = []
        if e not in data[g][gen]: data[g][gen].append(e)
    return jsonify(data)

@app.route('/api/get_event_start_list', methods=['POST'])
def get_event_start_list():
    data = request.json
    conn = get_db_connection()
    c = conn.cursor()

    event_name = data.get('event_name')
    # 🔍 核心修复：判断是否为接力项目
    is_relay = '4x' in event_name or '接力' in event_name

    if is_relay:
        # 🏃 接力项目：不再按“姓名”匹配，而是按“班级+项目”匹配
        # 使用 MIN(r.id) 确保一个班级只对应一个 reg_id，方便录入
        sql = """
            SELECT 
                s.*, 
                MAX(r.score) as score, 
                MIN(r.id) as reg_id 
            FROM start_list s
            LEFT JOIN registrations r ON 
                s.team_name = r.team_name AND 
                s.event_name = r.event_name
            WHERE s.event_name = ?
        """
    else:
        # 👤 个人项目：保持原有的姓名精确匹配
        sql = """
            SELECT 
                s.*, 
                r.score, 
                r.id as reg_id 
            FROM start_list s
            LEFT JOIN registrations r ON 
                s.name = r.name AND 
                s.team_name = r.team_name AND 
                s.event_name = r.event_name
            WHERE s.event_name = ?
        """
    
    p = [event_name]
    if data.get('group_name'):
        sql += " AND s.group_name = ?"
        p.append(data['group_name'])
        
    sql += " GROUP BY s.id ORDER BY CAST(s.heat AS INTEGER) ASC, CAST(s.lane AS INTEGER) ASC"
    
    try:
        rows = c.execute(sql, p).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()

@app.route('/api/submit_score', methods=['POST'])
def submit_score():
    data = request.json
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        raw_val = str(data.get('score', '')).strip()
        reg_id = data.get('id')
        
        # 🚀 修复点：确保 event_name 和 team_name 都能从数据库补全
        event_name = data.get('event_name', '')
        team_name = data.get('team_name', '')
        name = data.get('name', '')
        if reg_id:
            row = c.execute("SELECT event_name, team_name FROM registrations WHERE id=?", (reg_id,)).fetchone()
            if row:
                event_name = row['event_name']
                team_name = row['team_name']
        else:
            team_name = data.get('team_name', '')

        formatted_score = raw_val # 默认保留原样

        if raw_val:
            # --- 2. 项目类型识别 (保留你原有的 1050-1070 行逻辑) ---
            is_field = False
            field_keywords = ['跳', '投', '掷', '铅球', '实心球', '标枪', '铁饼', '球', '引体', '仰卧']
            
            # 查配置表确定类型
            cfg = c.execute("SELECT type FROM cfg_events WHERE name=?", (event_name,)).fetchone()
            if not cfg:
                core = re.sub(r"\(.*?\)|（.*?）|决赛|预赛|男子|女子|男|女", "", event_name).strip()
                cfg = c.execute("SELECT type FROM cfg_events WHERE name=?", (core,)).fetchone()

            if cfg and (cfg['type'] == '田赛' or '田' in str(cfg['type'])):
                is_field = True
            elif any(kwd in event_name for kwd in field_keywords):
                is_field = True
            
            # 识别中长跑 (含400米和接力)
            is_middle_long = any(x in event_name for x in ['400', '800', '1000', '1500', '3000', '5000', '4x', '4×'])

            # --- 3. 格式化处理 (保留你原有的 1075-1100 行核心逻辑) ---
            if is_field:
                # 【田赛】: 3.45 -> 3.45 (冒号变点)
                formatted_score = raw_val.replace(':', '.').replace('：', '.')
                if formatted_score.count('.') > 1:
                     parts = formatted_score.split('.')
                     formatted_score = f"{parts[0]}.{parts[1]}"
            else:
                # 【径赛】: 处理冒号、双点号及中长跑缩写
                if ':' in raw_val or '：' in raw_val:
                    formatted_score = raw_val.replace('：', ':')
                elif raw_val.count('.') == 2:
                    parts = raw_val.split('.')
                    formatted_score = f"{parts[0]}:{parts[1]}.{parts[2]}"
                elif is_middle_long:
                    try:
                        val_float = float(raw_val)
                        if val_float < 12: # 模式: 1.05 -> 1:05.00
                            if '.' in raw_val:
                                parts = raw_val.split('.')
                                minute, second = parts[0], parts[1]
                                if len(second) == 1: second += "0"
                                formatted_score = f"{minute}:{second}.00"
                            else: # 模式: 1 -> 1:00.00
                                formatted_score = f"{raw_val}:00.00"
                        else: # 大于12认为是秒数，保持原样
                            formatted_score = raw_val
                    except: pass 
                else:
                    formatted_score = raw_val

        # --- 4. 执行更新 (✨ 核心修改点：集成团体/接力同步逻辑) ---
        # 判断是否为接力或团体项目
        is_relay = re.search(r'4[xX*]|接力', event_name) is not None
        
        if is_relay:
            c.execute("UPDATE registrations SET score = ? WHERE team_name = ? AND event_name = ?", (formatted_score, team_name, event_name))
        else:
            if reg_id:
                c.execute("UPDATE registrations SET score = ? WHERE id = ?", (formatted_score, reg_id))
            else:
                c.execute("UPDATE registrations SET score = ? WHERE name = ? AND event_name = ? AND team_name = ?", (formatted_score, name, event_name, team_name))
            
        conn.commit()
        return jsonify({"status": "success", "msg": "已保存", "new_score": formatted_score})
    except Exception as e:
        import traceback; traceback.print_exc()
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
@app.route('/api/publish_finals', methods=['POST'])
def publish_finals():
    data = request.json
    display_name = data.get('final_event_name') 
    g_name = data.get('group_name')             
    gender = data.get('gender')                 
    athletes = data.get('athletes')

    if not athletes: return jsonify({"status": "error", "msg": "名单为空"})

    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        g_info = c.execute("SELECT id FROM cfg_groups WHERE name=?", (g_name,)).fetchone()
        gid = g_info['id'] if g_info else 0

        c.execute("DELETE FROM registrations WHERE group_name=? AND event_name=? AND gender=?", (g_name, display_name, gender))
        c.execute("DELETE FROM start_list WHERE group_name=? AND event_name=? AND gender=?", (g_name, display_name, gender))

        for i, ath in enumerate(athletes):
            lane = str(i + 1)
            # 1. 写入计分表
            c.execute("""INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, score)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')""", 
                      (gid, g_name, ath.get('team_id', 0), ath.get('team_name', ''), ath.get('name', ''), gender, ath.get('bib', ''), display_name))
            # 2. 写入裁判表
            c.execute("""INSERT INTO start_list (group_name, event_name, gender, heat, lane, bib, name, team_name, type)
                         VALUES (?, ?, ?, '1', ?, ?, ?, ?, '径赛')""",
                      (g_name, display_name, gender, lane, ath.get('bib', ''), ath.get('name', ''), ath.get('team_name', '')))
        
        conn.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()

@app.route('/api/manage_team_passwords', methods=['POST'])
def manage_team_passwords():
    action = request.json.get('action')
    conn = get_db_connection() # 使用带 WAL 模式的连接
    c = conn.cursor()

    if action == 'generate':
        teams = set()
        try:
            # 扫描所有代表队
            for r in c.execute("SELECT name FROM cfg_teams").fetchall(): teams.add(r['name'])
            for r in c.execute("SELECT DISTINCT team_name FROM registrations WHERE team_name != ''").fetchall(): teams.add(r['team_name'])
            
            # 扫描并生成缺少的密码
            for team in teams:
                if not c.execute("SELECT 1 FROM team_auth WHERE team_name=?", (team,)).fetchone():
                    new_pass = ''.join(random.choices(string.digits, k=6))
                    c.execute("INSERT INTO team_auth (team_name, password) VALUES (?, ?)", (team, new_pass))
            conn.commit()
        except Exception as e:
            print(f"生成错误: {e}")

    # ⭐ 核心：使用 JOIN 关联组别，实现按组别排序输出
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
    data = request.json
    g_name = data.get('group_name') 
    gender = data.get('gender')      
    base_evt = data.get('event') 
    top_n = int(data.get('top_n', 8))

    conn = get_db_connection()
    c = conn.cursor()
    try:
        clean_core = re.sub(r"男子|女子|混合", "", base_evt).strip()
        row = c.execute("SELECT has_prelim FROM cfg_events WHERE name = ?", (clean_core,)).fetchone()
        if not row: # 模糊匹配兜底
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
              AND event_name LIKE ? 
              AND event_name NOT LIKE '%决赛%'
              AND score != '' AND score IS NOT NULL
        """
        rows = c.execute(query, (g_name, gender, f"%{clean_core}%")).fetchall()
        athletes = [dict(r) for r in rows]
        
        if not athletes:
            return jsonify({"status": "error", "msg": "未找到有效的预赛成绩，无法生成决赛名单"})

        def parse_time(val):
            try:
                s = str(val).strip().replace('：', ':').replace('。', '.')
                if ':' in s:
                    p = s.split(':')
                    return float(p[0])*60 + float(p[1])
                return float(s)
            except: return 99999.0
        
        athletes.sort(key=lambda x: parse_time(x['score']))

        prefix = "女子" if gender == '女' else ("男子" if gender == '男' else gender)
        final_display_name = f"{prefix}{clean_core}决赛" 
        
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
# 📥 导入导出接口 (全)
# ============================================================

# ✅ 补充：系统全量备份接口
@app.route('/api/export_system')
def export_system():
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; c = conn.cursor()
    data = {
        "groups": [dict(r) for r in c.execute("SELECT * FROM cfg_groups").fetchall()],
        "teams": [dict(r) for r in c.execute("SELECT * FROM cfg_teams").fetchall()],
        "events": [dict(r) for r in c.execute("SELECT * FROM cfg_events").fetchall()],
        "config": {r['key']: r['value'] for r in c.execute("SELECT * FROM sys_config").fetchall()},
        "registrations": [dict(r) for r in c.execute("SELECT * FROM registrations").fetchall()]
    }
    conn.close()
    mem = BytesIO(); mem.write(json.dumps(data, ensure_ascii=False).encode('utf-8')); mem.seek(0)
    return send_file(mem, mimetype='application/json', as_attachment=True, download_name=f'运动会系统备份_{datetime.now().strftime("%Y%m%d%H%M")}.json')

# ✅ 补充：系统全量恢复接口
@app.route('/api/import_system', methods=['POST'])
def import_system():
    if 'file' not in request.files: return jsonify({"status": "error", "msg": "未上传文件"})
    file = request.files['file']
    try:
        data = json.load(file)
        conn = sqlite3.connect(DB_FILE); c = conn.cursor()
        
        c.execute("DELETE FROM cfg_groups"); c.executemany("INSERT INTO cfg_groups (id, name, prefix) VALUES (:id, :name, :prefix)", data.get('groups', []))
        c.execute("DELETE FROM cfg_teams"); c.executemany("INSERT INTO cfg_teams (id, group_id, name, leader) VALUES (:id, :group_id, :name, :leader)", data.get('teams', []))
        c.execute("DELETE FROM cfg_events"); c.executemany("INSERT INTO cfg_events (id, name, type, gender, score_rule, record, record_bonus, is_double_score, need_lane, has_prelim, is_relay, limit_count, allowed_groups) VALUES (:id, :name, :type, :gender, :score_rule, :record, :record_bonus, :is_double_score, :need_lane, :has_prelim, :is_relay, :limit_count, allowed_groups)", data.get('events', []))
        c.execute("DELETE FROM sys_config"); c.executemany("INSERT INTO sys_config (key, value) VALUES (?, ?)", [(k,v) for k,v in data.get('config', {}).items()])
        c.execute("DELETE FROM registrations"); c.executemany("INSERT INTO registrations (id, group_id, group_name, team_id, team_name, name, gender, bib, event_name, score, rank, lane, heat, submit_time) VALUES (:id, :group_id, :group_name, :team_id, :team_name, :name, :gender, :bib, :event_name, :score, :rank, :lane, :heat, :submit_time)", data.get('registrations', []))
        
        conn.commit(); return jsonify({"status": "success", "msg": "✅ 备份数据恢复成功！"})
    except Exception as e: return jsonify({"status": "error", "msg": "恢复失败: " + str(e)})
    finally: conn.close()

@app.route('/api/export_registrations')
def export_registrations():
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    try: rows = c.execute("SELECT group_name, team_name, name, gender, bib, event_name FROM registrations").fetchall()
    except Exception as e: return f"导出错误: {str(e)}"
    finally: conn.close()

    athletes_map = {}; max_event_count = 0
    for r in rows:
        g_name, t_name, name, gender, bib, evt = r
        key = f"{g_name}_{t_name}_{name}"
        if key not in athletes_map: athletes_map[key] = {'group': g_name, 'team': t_name, 'name': name, 'gender': gender, 'bib': bib, 'events': []}
        if evt:
            athletes_map[key]['events'].append(evt)
            if len(athletes_map[key]['events']) > max_event_count: max_event_count = len(athletes_map[key]['events'])

    if max_event_count < 3: max_event_count = 3
    output = StringIO(); output.write('\ufeff'); writer = csv.writer(output)
    headers = ['组别', '代表队', '姓名', '性别', '号码'] + [f'项目{i+1}' for i in range(max_event_count)]
    writer.writerow(headers)
    
    for p in athletes_map.values():
        row = [p['group'], p['team'], p['name'], p['gender'], p['bib']] + p['events']
        row.extend([''] * (max_event_count - len(p['events'])))
        writer.writerow(row)
        
    mem = BytesIO(); mem.write(output.getvalue().encode('utf-8-sig')); mem.seek(0)
    return send_file(mem, mimetype='text/csv', as_attachment=True, download_name=f'报名名单_{datetime.now().strftime("%Y%m%d")}.csv')

@app.route('/api/import_registrations', methods=['POST'])
def import_registrations():
    if 'file' not in request.files: return jsonify({"status": "error", "msg": "未上传文件"})
    file = request.files['file']
    if not file.filename.endswith('.csv'): return jsonify({"status": "error", "msg": "请上传 .csv 文件"})

    try:
        # 使用 UTF-8-SIG 读取防止 BOM 问题
        stream = StringIO(file.stream.read().decode("utf-8-sig"), newline=None)
        csv_input = csv.reader(stream)
        next(csv_input, None) # 安全跳过表头
        
        conn = get_db_connection() 
        c = conn.cursor()
        
        # 缓存配置数据，减少数据库查询
        groups_map = {row['name']: row['id'] for row in c.execute("SELECT id, name FROM cfg_groups").fetchall()}
        teams_map = {row['name']: row['id'] for row in c.execute("SELECT id, name FROM cfg_teams").fetchall()}
        
        # 缓存项目类型，避免循环内查询
        event_types = {row['name']: row['type'] for row in c.execute("SELECT name, type FROM cfg_events").fetchall()}
        
        sys_config = {row['key']: row['value'] for row in c.execute("SELECT key, value FROM sys_config").fetchall()}
        MAX_TOTAL = int(sys_config.get('maxTotal', 20))
        MAX_PER_EVENT = int(sys_config.get('maxPerEvent', 3))
        
        success_count = 0
        insert_buffer = []
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        c.execute("BEGIN IMMEDIATE") # 开启事务

        for row in csv_input:
            if len(row) < 4: continue 
            g_name, t_name, name, gender = row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip()
            bib = row[4].strip() if len(row) > 4 else ""
            
            gid = groups_map.get(g_name, 0)
            tid = teams_map.get(t_name, 0)
            
            if not gid or not tid: continue # 组别或班级不存在则跳过

            event_list = [item.strip() for col in row[5:] for item in col.replace('，', ',').split(',') if item.strip()]
            unique_events = list(set(event_list)) # 去重

            for sub_evt in unique_events:
                # 1. 检查是否已报名
                exists = c.execute("SELECT 1 FROM registrations WHERE team_id=? AND name=? AND event_name=?", (tid, name, sub_evt)).fetchone()
                if exists: continue
                
                # 2. 检查项目是否存在及类型
                evt_type = event_types.get(sub_evt)
                is_fun = evt_type and ('趣味' in str(evt_type))
                
                # 3. 检查单项限额 (非趣味项目)
                if not is_fun:
                    # 查找该项目的属性
                    evt_meta = c.execute("SELECT is_relay, gender FROM cfg_events WHERE name=?", (sub_evt,)).fetchone()
                    is_relay = to_bool_str(evt_meta['is_relay']) == '1' if evt_meta else False
                    is_mixed = (evt_meta['gender'] == '混合') if evt_meta else False
                    
                    if is_mixed and is_relay:
                        current_limit = 10
                    elif is_relay:
                        current_limit = 4
                    else:
                        current_limit = MAX_PER_EVENT
                    
                    curr_evt_count = c.execute("SELECT COUNT(*) FROM registrations WHERE team_id=? AND event_name=?", (tid, sub_evt)).fetchone()[0]
                    if curr_evt_count >= current_limit: 
                        continue
                
                # 4. 检查班级总人数限额
                is_new_athlete = not c.execute("SELECT 1 FROM registrations WHERE team_id=? AND name=?", (tid, name)).fetchone()
                if is_new_athlete:
                     curr_team_total = c.execute("SELECT COUNT(DISTINCT name) FROM registrations WHERE team_id=?", (tid,)).fetchone()[0]
                     if curr_team_total >= MAX_TOTAL: break # 该人无法报名任何项目了

                # 5. 执行插入
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
basedir = os.path.abspath(os.path.dirname(__file__))
DB_FILE = os.path.join(basedir, 'sports_data.db')
init_db()
# app.py -> init_db() 内部末尾
def upgrade_records():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # 增加纪录值 (文本，如 1:55.00) 和 破纪录分值 (整数，如 2)
    try: c.execute("ALTER TABLE cfg_events ADD COLUMN record TEXT")
    except: pass
    try: c.execute("ALTER TABLE cfg_events ADD COLUMN record_bonus INTEGER DEFAULT 0")
    except: pass
    conn.commit()
    conn.close()

upgrade_records()
if __name__ == '__main__':
    
    try: 
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]; s.close()
    except: local_ip = "127.0.0.1"
    print(f"✅ 启动成功！")
    print(f"👉 领队端: http://{local_ip}:5000/login")
    print(f"👉 管理端: http://{local_ip}:5000/admin/login")
    print(f"👉 裁判端: http://{local_ip}:5000/referee/login")
    app.run(debug=True, host='0.0.0.0', port=5000)