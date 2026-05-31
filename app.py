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
                core = re.sub(r"\(.*?\)|（.*?）|决赛|预赛|及格赛|男子|女子|混合|男|女|第一组|第二组|第三组|第四组|第\d+组", "", evt).strip()
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

                # 🌟 核心突破：一次性拉取该项目所有轮次（预赛+决赛）的全部成绩，用于比对最高峰值！
                placeholders = ','.join(['?'] * len(sub_events))
                sql = f"SELECT id, name, team_name, event_name, score FROM registrations WHERE group_name=? AND gender=? AND event_name IN ({placeholders}) AND score != ''"
                all_data_rows = c.execute(sql, [g_name, gender] + sub_events).fetchall()
                if not all_data_rows: continue
                
                # 建立全赛程最佳成绩档案库
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
                
                # 剥离出仅用于排发名次分的决赛数据
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
                    
                    # 🚀 多级破纪录智能核算：用他的【全赛程最佳成绩】(best_val)来冲击纪录，而非仅仅是决赛成绩！
                    is_relay_event = re.search(r'4[xX*×]|接力', item['event_name']) is not None
                    key = f"TEAM_{item['team_name']}" if is_relay_event else f"ATH_{item['team_name']}_{item['name']}"
                    best_val = best_score_map.get(key, item['_val'])

                    max_bonus = 0
                    broken_code = ''
                    for rec in my_records:
                        if rec.get('en'): 
                            rec_val = parse_time_to_seconds(rec.get('val'))
                            r_bonus = int(rec.get('bonus') or 0)
                            if rec_val is not None and rec_val > 0 and best_val > 0:
                                is_broken = (best_val > rec_val) if is_field else (best_val < rec_val)
                                if is_broken and r_bonus >= max_bonus:
                                    max_bonus = r_bonus
                                    broken_code = rec.get('code', '') 
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
    data = request.json
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
 
    # 🚀 核心优化：单独抽出 record_bonus 破纪总分；并且金银铜的判定必须减去破纪录分，保证名次统计的绝对真实！
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
    """保存比赛起始日期"""
    conn = get_db_connection()
    c = conn.cursor()
    try:
        data = request.json or {}
        start_date = data.get('start_date', '') # 格式如 "2026-05-27"
        
        # 存入系统配置表 system_settings (如果没有该表则初始化)
        c.execute("""CREATE TABLE IF NOT EXISTS system_settings 
                     (key TEXT PRIMARY KEY, value TEXT)""")
        c.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES ('start_date', ?)", (start_date,))
        conn.commit()
        return jsonify({"success": True, "message": "比赛时间配置成功！"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        conn.close()

@app.route('/api/get_competition_date', methods=['GET'])
def get_competition_date():
    """获取比赛起始日期"""
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
@app.route('/api/get_team_score_details', methods=['POST'])
def get_team_score_details():
    data = request.json
    g_name = data.get('group_name')
    t_name = data.get('team_name')

    conn = get_db_connection()
    c = conn.cursor()

    try:
        # 1. 提取该组别下所有带有积分的记录，用于重新排定名次
        sql = """
            SELECT name, team_name, event_name, gender, score, points
            FROM registrations
            WHERE group_name = ? AND points > 0
        """
        rows = c.execute(sql, (g_name,)).fetchall()

        # 2. 按项目和性别分组，准备进行名次重算
        events_data = {}
        for r in rows:
            key = f"{r['event_name']}_{r['gender']}"
            if key not in events_data:
                events_data[key] = []
            events_data[key].append(dict(r))

        team_details = []

        # 3. 在各项目内部进行成绩排序和名次标定
        for key, items in events_data.items():
            event_name = items[0]['event_name']
            gender = items[0]['gender']
            
            # 判断田赛还是径赛
            is_field = False
            field_keywords = ['跳', '投', '掷', '铅球', '实心球', '标枪', '铁饼', '球', '引体', '仰卧']
            if any(kwd in event_name for kwd in field_keywords):
                is_field = True

            def parse_time(val):
                try:
                    s = str(val).strip().replace('：', ':').replace('。', '.')
                    if ':' in s:
                        p = s.split(':')
                        return float(p[0])*60 + float(p[1])
                    return float(s)
                except: return 0.0

            for item in items:
                item['_val'] = parse_time(item['score'])

            # 根据田径规则排序
            items.sort(key=lambda x: x['_val'], reverse=is_field)

            # 提取本班的积分贡献者并核算其真实名次
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

        # 按积分从高到低、项目名称排序，优先展示高分项
        team_details.sort(key=lambda x: (-x['points'], x['event_name']))
        return jsonify(team_details)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify([])
    finally:
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
    return redirect('/bm')
@app.route('/admin/login')
def admin_login():
    return render_template('admin_login.html')
@app.route('/')
@app.route('/admin')
@login_required('admin')
def admin():
    local_ip = get_host_ip()
    # 补充传入 user_team_id 和 team_name，防止前端 JS 报错
    return render_template('admin.html', 
                           local_ip=local_ip,
                           user_team_id=session.get('team_id', ''),
                           team_name=session.get('team_name', ''))

@app.route('/bm')
def bm_page():
    """独立报名首页（未登录时由模板自己展示登录框）"""
    # 无论是谁请求，直接渲染 bm.html。它内部有 {% if user_team_id %} 来做分流
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
        username = data.get('username')
        password = data.get('password')
        
        # ✨ 增加判断：登录名必须是 admin，密码必须匹配
        if username == 'admin' and password == ADMIN_PASSWORD:
            session['user_role'] = 'admin'
            return jsonify({'status': 'success', 'redirect': '/admin'})
        else:
            return jsonify({'status': 'fail', 'msg': '认证失败：登录名或密码错误'})
    elif role_type == 'referee':
        # 👉 新增：获取登录名并校验
        username = data.get('username')
        if username == 'referee' and data.get('password') == REFEREE_PASSWORD:
            session['user_role'] = 'referee'
            return jsonify({'status': 'success', 'redirect': '/referee'})
        else:
            return jsonify({'status': 'fail', 'msg': '认证失败：登录名或密码错误'})
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
        return jsonify({'status': 'success', 'redirect': '/bm'})
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
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM registrations")
        c.execute("DELETE FROM start_list")
        c.execute("DELETE FROM team_auth")
 
        if mode == 'all':
            c.execute("DELETE FROM cfg_groups")
            c.execute("DELETE FROM cfg_teams")
            c.execute("DELETE FROM cfg_events")
            c.execute("DELETE FROM sqlite_sequence") # 重置自增 ID
            
        conn.commit()
        return jsonify({"status": "success", "msg": "系统已成功重置，新版表结构已强行初始化。"})
    except Exception as e:
        conn.rollback()
        import traceback; traceback.print_exc()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
        # 强力核心：在重置后立刻执行升级补丁，确保 total_lanes 等字段 100% 被建立
        force_sync_and_upgrade_db()

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
    conn = get_db_connection()
    c = conn.cursor()
    try:
        db_groups = [dict(r) for r in c.execute("SELECT * FROM cfg_groups").fetchall()]
        db_teams = [dict(r) for r in c.execute("SELECT * FROM cfg_teams").fetchall()]
        for t in db_teams: t['groupId'] = t['group_id']
        db_events = [dict(r) for r in c.execute("SELECT * FROM cfg_events").fetchall()]
        
        # 🌟 核心修复：从物理数据库(start_list表)中，把已经成功编排的数据一条不少地捞出来！
        db_schedule = []
        try:
            raw_sch = c.execute('''SELECT id, group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field 
                                 FROM start_list ORDER BY time_index ASC, CAST(heat AS INTEGER) ASC, CAST(lane AS INTEGER) ASC''').fetchall()
            for r in raw_sch:
                item = dict(r)
                item['groupName'] = r['group_name']
                item['eventName'] = r['event_name']
                item['teamName'] = r['team_name']
                item['isField'] = (r['is_field'] == 1)
                
                # ✅ 补齐前端需要的驼峰命名时间字段映射
                item['estTime'] = r['est_time']
                item['timeIndex'] = r['time_index']
                item['totalLanes'] = r['total_lanes']
                db_schedule.append(item)
        except Exception as err: 
            print(f"读取编排表异常，可能缺少字段: {err}")
        
        raw_regs = c.execute("SELECT * FROM registrations").fetchall()
        athletes_map = {}
        for r in raw_regs:
            key = f"{r['team_id']}_{r['name']}"
            if key not in athletes_map:
                # ✨ 核心修复：在这里增加 "relay_legs": {}
                athletes_map[key] = { "id": r['id'], "teamId": int(r['team_id']) if r['team_id'] else 0, "name": r['name'], "gender": r['gender'], "bib": r['bib'] or "", "events": [], "relay_legs": {} }
            
            athletes_map[key]["events"].append(r['event_name'])
            
            # 安全地写入接力棒次数据
            try:
                if 'relay_leg' in r.keys() and r['relay_leg']:
                    athletes_map[key]["relay_legs"][r['event_name']] = str(r['relay_leg'])
            except Exception as e:
                print(f"解析接力棒次异常: {e}") # 改为打印错误，不再静默吞噬
        config = {r['key']: r['value'] for r in c.execute("SELECT * FROM sys_config").fetchall()}
        
        return jsonify({
            "status": "success",
            "groups": db_groups, 
            "teams": db_teams, 
            "events": db_events, 
            "athletes": list(athletes_map.values()), 
            "config": config, 
            "schedule": db_schedule  # 🌟 将干净的编排大名单强行下发给前端，绝不洗白！
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
@app.route('/api/save_relay_legs', methods=['POST'])
def save_relay_legs():
    if session.get('user_role') != 'team':
        return jsonify({"status": "error", "msg": "权限不足"}), 403
        
    data = request.json
    team_id = data.get('team_id')
    event_name = data.get('event_name')
    gender = data.get('gender')
    legs = data.get('legs') # 数据结构：{"1": "报名记录ID", "2": "报名记录ID"...}
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        # 先清空该接力项目本班的所有旧棒次
        c.execute("UPDATE registrations SET relay_leg = '' WHERE team_id=? AND event_name=? AND gender=?", (team_id, event_name, gender))
        # 重新写入新棒次
        for leg_num, reg_id in legs.items():
            if reg_id:
                c.execute("UPDATE registrations SET relay_leg = ? WHERE id = ?", (str(leg_num), int(reg_id)))
        conn.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "msg": str(e)})
    finally:
        conn.close()
@app.route('/api/save_config', methods=['POST'])
def save_config():
    data = request.json or {}
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        # 1. 级联保存组别 (彻底丢弃硬性 int 转换，改用 str 防御大数字溢出)
        if 'groups' in data:
            c.execute("DELETE FROM cfg_groups")
            for g in data['groups']:
                # ✨ 修复点：使用 str() 包裹 id，确保高精度时间戳 ID 能 100% 钉进数据库
                c.execute("INSERT OR REPLACE INTO cfg_groups (id, name, prefix) VALUES (?, ?, ?)", 
                          (str(g['id']), g['name'], g['prefix']))
                  
        # 2. 级联保存代表队
        if 'teams' in data:
            c.execute("DELETE FROM cfg_teams")
            for t in data['teams']:
                # ✨ 修复点：使用 str() 包裹 id 和 groupId，防止级联外键解析时丢失高位数据
                c.execute("INSERT OR REPLACE INTO cfg_teams (id, group_id, name, leader) VALUES (?, ?, ?, ?)", 
                          (str(t['id']), str(t['groupId']), t['name'], t.get('leader','')))
                  
        # 3. 级联保存项目矩阵
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
                    str(e['id']), 
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
                    str(e.get('allowedGroups', ''))
                )
                c.execute(sql, params)
                
        # 4. 全局参数配置保存
        if 'config' in data:
            for k, v in data['config'].items(): 
                c.execute("REPLACE INTO sys_config (key, value) VALUES (?, ?)", (k, str(v)))
                
        conn.commit()
        return jsonify({"status": "success", "msg": "✅ 配置及参赛单位已全量同步写入数据库！"})
    except Exception as e:
        conn.rollback()
        import traceback; traceback.print_exc() # 控制台打印错误日志，方便抓包
        return jsonify({"status": "error", "msg": "保存失败: " + str(e)})
    finally:
        conn.close()
# ✅ 补充：领队端查询本班名单接口
@app.route('/api/team_members/<int:team_id>')
def get_team_members(team_id):
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row; c = conn.cursor()
    rows = c.execute("SELECT id, name, gender, event_name, relay_leg FROM registrations WHERE team_id = ?", (team_id,)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])
@app.route('/api/batch_update_bibs', methods=['POST'])
def batch_update_bibs():
    """批量固化保存自动生成的运动员号码牌"""
    if 'user_role' not in session:
        return jsonify({"status": "error", "msg": "会话已过期，请重新登录"}), 401
        
    data = request.json or {}
    athletes = data.get('athletes', [])
    
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        for a in athletes:
            # 根据班级ID和姓名，批量更新该运动员在所有报项中的号码牌
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
    # ✨ 核心修复 1：兼容管理员和领队双重身份，防止管理员被拦截器强行踢出
    if 'user_role' not in session:
        return jsonify({"status": "error", "msg": "未登录或登录已过期，请重新登录！"}), 401
    
    data = request.json or {}
    user_role = session.get('user_role')
    
    # 如果是普通领队，限制其只能给自己班级报名，防止越权
    if user_role == 'team':
        if str(data.get('team_id')) != str(session.get('team_id')):
            return jsonify({"status": "error", "msg": "越权操作：领队只能为本班学生报名！"}), 403
            
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE") 

        # 👉 增加：报名截止时间拦截 (仅限领队拦截，管理员绝对放行)
        if user_role == 'team':
            deadline_row = c.execute("SELECT value FROM sys_config WHERE key='regDeadline'").fetchone()
            if deadline_row and deadline_row[0]:
                try:
                    # 解析前端传来的 datetime-local 格式 'YYYY-MM-DDTHH:MM'
                    deadline_dt = datetime.strptime(deadline_row[0], "%Y-%m-%dT%H:%M")
                    if datetime.now() > deadline_dt:
                        return jsonify({"status": "error", "msg": f"报名通道已关闭！截止时间为：{deadline_row[0].replace('T', ' ')}，如需特殊修改请联系系统管理员。"})
                except Exception as e:
                    pass
        
        # 统一从配置表读取限额参数
        def get_cfg_val(key, default):
            row = c.execute("SELECT value FROM sys_config WHERE key=?", (key,)).fetchone()
            return int(row[0]) if row else default
        
        MAX_PER_PERSON = get_cfg_val('maxPerPerson', 2)
        MAX_PER_EVENT = get_cfg_val('maxPerEvent', 3)
        MAX_TOTAL = get_cfg_val('maxTotal', 20)
        
        # ✨ 核心修复 2：全部统一转换为干净的标准字符串，彻底解决 SQLite 物理表 int/str 匹配错位冲突
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

        # 检查班级总人数限制（排除当前正在编辑的这个人，支持修改报名）
        current_team_count = c.execute("SELECT COUNT(DISTINCT name) FROM registrations WHERE team_id=? AND name!=?", (team_id, name)).fetchone()[0]
        if current_team_count >= MAX_TOTAL:
            return jsonify({"status": "error", "msg": f"报名失败！本班总人数已达上限（{MAX_TOTAL}人）！"})
            
        # 先清除该学生在该班级下的旧报名记录（实现覆盖/修改报名的全量同步功能）
        c.execute("DELETE FROM registrations WHERE team_id=? AND name=?", (team_id, name))
        
        # 逐项审查限额和适用性
        for evt in selected_events:
            evt_info = c.execute("SELECT type, is_relay, gender, limit_count FROM cfg_events WHERE name=?", (evt,)).fetchone()
            if not evt_info:
                # 模糊匹配兜底
                evt_info = c.execute("SELECT type, is_relay, gender, limit_count FROM cfg_events WHERE name LIKE ?", (f"%{evt}%",)).fetchone()
                
            if evt_info:
                # 1. 趣味项目跳过限额检查
                is_fun = (evt_info['type'] == '趣味' or '趣味' in str(evt_info['type']))
                if is_fun:
                    continue
                
               # 2. 动态确定接力/个人限额
                is_relay = (str(evt_info['is_relay']) == '1' or str(evt_info['is_relay']).lower() == 'true')
                if evt_info['gender'] == '混合' and is_relay:
                    current_limit = 10
                elif is_relay:
                    current_limit = 4
                else:
                    current_limit = MAX_PER_EVENT
                
                # 🌟 核心升级：按“班级 + 项目 + 性别”三维隔离统计已报人数
                count_in_evt = c.execute("SELECT COUNT(*) FROM registrations WHERE team_id=? AND event_name=? AND gender=?", (team_id, evt, gender)).fetchone()[0]
                if count_in_evt >= current_limit:
                    return jsonify({"status": "error", "msg": f"项目【{evt}】本班【{gender}生】名额（限报 {current_limit} 人）已满，无法报名！"})

        # 从基础表中反查组别和代表队的真实名称
        g_info = c.execute("SELECT name FROM cfg_groups WHERE id=?", (group_id,)).fetchone()
        t_info = c.execute("SELECT name FROM cfg_teams WHERE id=?", (team_id,)).fetchone()
        g_name = g_info[0] if g_info else "未知组别"
        t_name = t_info[0] if t_info else "未知班级"
        submit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 批量灌入报名数据
        for evt in selected_events:
            c.execute("""INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, submit_time) 
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", 
                      (group_id, g_name, team_id, t_name, name, gender, bib, evt, submit_time))
        
        conn.commit()
        return jsonify({"status": "success", "msg": "🎉 报名信息已成功录入系统！"})
        
    except Exception as e:
        conn.rollback()
        import traceback; traceback.print_exc() # 会在你的 Python 终端控制台打印爆破日志
        return jsonify({"status": "error", "msg": f"数据库写入异常: {str(e)}"})
    finally:
        conn.close()
# ✅ 补充：发布编排结果给裁判
@app.route('/api/save_schedule_to_db', methods=['POST'])
def save_schedule_to_db():
    schedule_data = request.json
    if not schedule_data: 
        return jsonify({"status": "error", "msg": "没有接收到合法的编排名单数据"})
        
    db_p = os.path.join(BASE_DIR, "data", "sports_data.db")
    conn = sqlite3.connect(db_p)
    c = conn.cursor()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM start_list")
        
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
                (group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (g_name, e_name, gender, heat, lane, bib, name, team_name, evt_type, total_lanes, est_time, time_index, is_field))
            
        conn.commit()
        return jsonify({"status": "success", "msg": "发布成功"})
    except Exception as e:
        try:
            conn.rollback()
        except:
            pass
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "msg": "写入数据库失败: " + str(e)})
    finally:
        conn.close()
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

    event_name = data.get('event_name') or ''
    group_name = data.get('group_name') or ''
    gender = data.get('gender') or ''
    
    is_relay = '4x' in event_name or '4×' in event_name or '接力' in event_name
    
    if ' (预赛)' in event_name:
        target_reg_event = event_name.replace(' (预赛)', '')
    else:
        target_reg_event = event_name

    # 🚀 在下发名单时，一并挂载后端已经通过“全赛程峰值”算好的积分 (points) 和破纪加分 (record_bonus)
    if is_relay:
        sql = """
            SELECT 
                s.id as s_id, s.group_name, s.event_name, s.gender, s.heat, s.lane, s.bib, s.name, s.team_name, s.type, s.total_lanes, s.est_time, s.time_index, s.is_field,
                MAX(r.score) as score,
                MIN(r.id) as reg_id,
                MAX(r.points) as points,
                MAX(r.record_bonus) as record_bonus
            FROM start_list s
            LEFT JOIN registrations r ON s.team_name = r.team_name AND r.event_name = ?
            WHERE s.event_name = ?
        """
        p = [target_reg_event, event_name]
    else:
        sql = """
            SELECT 
                s.id as s_id, s.group_name, s.event_name, s.gender, s.heat, s.lane, s.bib, s.name, s.team_name, s.type, s.total_lanes, s.est_time, s.time_index, s.is_field,
                r.score,
                r.id as reg_id,
                r.points,
                r.record_bonus
            FROM start_list s
            LEFT JOIN registrations r ON s.name = r.name AND s.team_name = r.team_name AND r.event_name = ?
            WHERE s.event_name = ?
        """
        p = [target_reg_event, event_name]
    
    if group_name:
        sql += " AND s.group_name = ?"
        p.append(group_name)
        
    if gender:
        sql += " AND s.gender = ?"
        p.append(gender)
        
    sql += " GROUP BY s.id ORDER BY CAST(s.heat AS INTEGER) ASC, CAST(s.lane AS INTEGER) ASC"
    
    try:
        rows = c.execute(sql, p).fetchall()
        res_list = []
        for r in rows:
            item = dict(r)
            item['id'] = item.pop('s_id')
            res_list.append(item)
        return jsonify(res_list)
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
        reg_id = data.get('id')  # 这里接的是刚才下发的纯净版 reg_id
        
        if not reg_id:
            conn.close()
            return jsonify({"status": "error", "msg": "记录异常(缺少报名ID)，请刷新页面重试"})

        # 反查是为了拿项目名来进行成绩精算判断
        row = c.execute("SELECT event_name, team_name, name FROM registrations WHERE id=?", (reg_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({"status": "error", "msg": "未找到对应的报名记录，无法保存"})

        event_name = row['event_name']
        team_name = row['team_name']
        formatted_score = raw_val

        if raw_val:
            # --- 成绩精算与格式化 ---
            is_field = False
            field_keywords = ['跳', '投', '掷', '铅球', '实心球', '标枪', '铁饼', '球', '引体', '仰卧']
            
            cfg = c.execute("SELECT type FROM cfg_events WHERE name=?", (event_name.replace(' (预赛)','').replace(' (决赛)','').strip(),)).fetchone()
            if cfg and (cfg['type'] == '田赛' or '田' in str(cfg['type'])): 
                is_field = True
            elif any(kwd in event_name for kwd in field_keywords): 
                is_field = True
            
            is_middle_long = any(x in event_name for x in ['400', '800', '1000', '1500', '3000', '5000', '4x', '4×'])

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
                elif is_middle_long:
                    try:
                        val_float = float(raw_val)
                        if val_float < 12:
                            if '.' in raw_val:
                                parts = raw_val.split('.')
                                minute, second = parts[0], parts[1]
                                if len(second) == 1: second += "0"
                                formatted_score = f"{minute}:{second}.00"
                            else:
                                formatted_score = f"{raw_val}:00.00"
                        else:
                            formatted_score = raw_val
                    except: pass 
                else:
                    formatted_score = raw_val

        # --- 🚀 执行精确更新 ---
        # 直接使用精确匹配出的 reg_id 写入对应赛次，彻底杜绝预决赛串线！
        is_relay = re.search(r'4[xX*×]|接力', event_name) is not None
        if is_relay:
            c.execute("UPDATE registrations SET score = ? WHERE team_name = ? AND event_name = ?", (formatted_score, team_name, event_name))
        else:
            c.execute("UPDATE registrations SET score = ? WHERE id = ?", (formatted_score, reg_id))
            
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

        # ✨ 核心修复：在删除占位符前，先安全提取它的比赛时间、排版等元数据，防止决赛替换后从沙盘消失
        dummy_meta = c.execute("SELECT est_time, time_index, total_lanes, type, is_field FROM start_list WHERE group_name=? AND event_name=? AND gender=? LIMIT 1", (g_name, display_name, gender)).fetchone()
        
        est_time = dummy_meta['est_time'] if dummy_meta else ''
        time_index = dummy_meta['time_index'] if dummy_meta else 0
        total_lanes = dummy_meta['total_lanes'] if dummy_meta else 8
        evt_type = dummy_meta['type'] if dummy_meta else 'sprint'
        is_field = dummy_meta['is_field'] if dummy_meta else 0

        c.execute("DELETE FROM registrations WHERE group_name=? AND event_name=? AND gender=?", (g_name, display_name, gender))
        c.execute("DELETE FROM start_list WHERE group_name=? AND event_name=? AND gender=?", (g_name, display_name, gender))

        for i, ath in enumerate(athletes):
            lane = str(ath.get('finalLane', i + 1))
            
            # 1. 写入计分主表
            c.execute("""INSERT INTO registrations (group_id, group_name, team_id, team_name, name, gender, bib, event_name, score)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, '')""", 
                      (gid, g_name, ath.get('team_id', 0), ath.get('team_name', ''), ath.get('name', ''), gender, ath.get('bib', ''), display_name))
            
            # 2. 写入裁判表 (携带刚才备份下来的完整的日程沙盘元数据)
            c.execute("""INSERT INTO start_list (group_name, event_name, gender, heat, lane, bib, name, team_name, type, total_lanes, est_time, time_index, is_field)
                         VALUES (?, ?, ?, '1', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                      (g_name, display_name, gender, lane, ath.get('bib', ''), ath.get('name', ''), ath.get('team_name', ''), evt_type, total_lanes, est_time, time_index, is_field))
        
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
        # 1. 彻底清洗项目名称，剥离出真正的核心名（例如："100米"）
        clean_core = re.sub(r"男子|女子|混合", "", base_evt).strip()
        clean_core = clean_core.replace(' (预赛)', '').replace(' (决赛)', '').replace('()', '').replace('（）', '').strip()

        row = c.execute("SELECT has_prelim FROM cfg_events WHERE name = ?", (clean_core,)).fetchone()
        if not row: # 模糊匹配兜底
            row = c.execute("SELECT has_prelim FROM cfg_events WHERE name LIKE ?", (f"%{clean_core}%",)).fetchone()
        if row:
            is_prelim = (str(row['has_prelim']) == '1' or str(row['has_prelim']).lower() == 'true')
            if not is_prelim:
                return jsonify({"status": "error", "msg": f"【{clean_core}】是直接决赛项目，无需生成决赛表！"})

        # 2. 💥核心修复：精准匹配报名表中的原名（100米）或已存的带后缀名（100米 (预赛)）
        query = """
            SELECT id, team_id, team_name, name, gender, bib, score 
            FROM registrations 
            WHERE group_name = ? 
              AND gender = ? 
              AND (event_name = ? OR event_name = ?)
              AND score != '' AND score IS NOT NULL
        """
        rows = c.execute(query, (g_name, gender, clean_core, f"{clean_core} (预赛)")).fetchall()
        athletes = [dict(r) for r in rows]
        
        if not athletes:
            return jsonify({"status": "error", "msg": "未找到有效的预赛成绩，请确认裁判是否已保存成绩！"})

        # 3. 智能解析成绩进行排序
        def parse_time(val):
            try:
                s = str(val).strip().replace('：', ':').replace('。', '.')
                if ':' in s:
                    p = s.split(':')
                    return float(p[0])*60 + float(p[1])
                return float(s)
            except: return 99999.0
        
        # 自动区分田赛(越大越好)和径赛(越小越快)
        is_field = clean_core.endswith('跳远') or clean_core.endswith('跳高') or clean_core.endswith('铅球') or clean_core.endswith('实心球') or clean_core.endswith('标枪')
        athletes.sort(key=lambda x: parse_time(x['score']), reverse=is_field)

        # 4. 💥核心修复：精准复原决赛项目的标准沙盘名称格式，确保与 start_list 中的占位符完全匹配
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
                # 1. 检查是否已报名该项目
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
                    
                    # ✨ 核心修复点 1：将限额统计升级为男女独立反查，只有同班、同项目且【同性别】的才会计数
                    curr_evt_count = c.execute(
                        "SELECT COUNT(*) FROM registrations WHERE team_id=? AND event_name=? AND gender=?", 
                        (tid, sub_evt, gender)
                    ).fetchone()[0]
                    
                    if curr_evt_count >= current_limit: 
                        continue # 项目超额，跳过此项目的录入
                
                # 4. 检查班级总人数限额
                is_new_athlete = not c.execute("SELECT 1 FROM registrations WHERE team_id=? AND name=?", (tid, name)).fetchone()
                if is_new_athlete:
                     curr_team_total = c.execute("SELECT COUNT(DISTINCT name) FROM registrations WHERE team_id=?", (tid,)).fetchone()[0]
                     # ✨ 核心修复点 2：将原有的 break 完美改为 continue！
                     # 班级满了只代表当前这个人进不去，不能卡死后续其他班级的正常导入循环
                     if curr_team_total >= MAX_TOTAL: 
                         continue 

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
def init_db():
    """补齐全局缺失的基础物理表结构组件函数"""
    conn = sqlite3.connect(DB_FILE)
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
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # 增加纪录值 (文本，如 1:55.00) 和 破纪录分值 (整数，如 2)
    try: c.execute("ALTER TABLE cfg_events ADD COLUMN record TEXT")
    except: pass
    try: c.execute("ALTER TABLE cfg_events ADD COLUMN record_bonus INTEGER DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE cfg_events ADD COLUMN duration REAL DEFAULT 5")
    except: pass
    conn.commit()
    conn.close()

def force_sync_and_upgrade_db():
    """终极强力数据库字段对齐补丁"""
    import sqlite3
    db_p = os.path.join(BASE_DIR, "data", "sports_data.db")
    if not os.path.exists(os.path.dirname(db_p)):
        os.makedirs(os.path.dirname(db_p))
        
    conn = sqlite3.connect(db_p)
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
        score TEXT DEFAULT ''
    )''')
    
    for col, t in [("total_lanes", "INTEGER DEFAULT 8"), ("est_time", "TEXT DEFAULT ''"), ("time_index", "INTEGER DEFAULT 0"), ("is_field", "INTEGER DEFAULT 0"), ("score", "TEXT DEFAULT ''")]:
        try: c.execute(f"ALTER TABLE start_list ADD COLUMN {col} {t}")
        except: pass

    # 🚀 强力修复：确保报名表中一定存在 points 积分列！解决报错 no such column: points
    try: c.execute("ALTER TABLE registrations ADD COLUMN points INTEGER DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE registrations ADD COLUMN relay_leg TEXT DEFAULT ''")
    except: pass    
    conn.commit()
    conn.close()
@app.route('/api/get_group_records', methods=['POST'])
def get_group_records():
    data = request.json
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
    data = request.json
    g_name = data.get('group_name')
    records_dict = data.get('records')
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
# 确保表结构初始化执行
init_db()
# 强行在 Flask 业务拉起前执行
force_sync_and_upgrade_db()
upgrade_records()
@app.route('/api/batch_save_events', methods=['POST'])
def batch_save_events():
    data = request.get_json() or {}
    events = data.get('events', [])
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        try: c.execute("ALTER TABLE cfg_events ADD COLUMN is_fun TEXT DEFAULT '0'")
        except: pass
        try: c.execute("ALTER TABLE cfg_events ADD COLUMN allowed_groups TEXT DEFAULT ''")
        except: pass
        try: c.execute("ALTER TABLE cfg_events ADD COLUMN duration REAL DEFAULT 5") # 补齐列
        except: pass
        
        c.execute("DELETE FROM cfg_events")
        
        for i, e in enumerate(events):
            has_prelim_str = '1' if e.get('hasPrelim') else '0'
            is_relay_str = '1' if e.get('isRelay') else '0'
            need_lane_str = '1' if e.get('needLane') else '0'
            is_fun_str = '1' if e.get('isFun', False) else '0'
            
            # 👇 SQL 中加入 duration
            c.execute('''
                INSERT INTO cfg_events 
                (id, name, type, gender, score_rule, record, record_bonus, is_double_score, need_lane, has_prelim, is_relay, limit_count, is_fun, allowed_groups, duration)
                VALUES (?, ?, ?, ?, ?, '', ?, '0', ?, ?, ?, ?, ?, ?, ?)
            ''', (
                str(i + 1), 
                e['name'], 
                e['type'], 
                e.get('gender', '双性'),
                e.get('scoreRule', '9,7,6,5,4,3,2,1'), 
                e.get('recordBonus', 0), 
                need_lane_str, 
                has_prelim_str, 
                is_relay_str, 
                e.get('limit', 8), 
                is_fun_str,
                str(e.get('allowedGroups', '')),
                float(e.get('duration', 5)) # 👇 保存耗时
            ))
            
        conn.commit()
        return jsonify({"success": True, "message": "✅ 项目选用矩阵及性别规则已全量同步到后台数据库！"})
    except Exception as ex:
        conn.rollback()
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "message": str(ex)})
    finally:
        conn.close()
if __name__ == '__main__':
    try: 
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except: 
        local_ip = "127.0.0.1"
        
    print(f"✅ 启动成功！")
    print(f"👉 领队端: http://{local_ip}:5000/team")
    print(f"👉 管理端: http://{local_ip}:5000/admin/login")
    print(f"👉 裁判端: http://{local_ip}:5000/referee/login")
    
    # 💥 核心修复点 1：强行关闭 Jinja2 的模板缓存
    app.jinja_env.auto_reload = True
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    
    # 💥 核心修复点 2：确保 debug=True 开启（如果你直接运行 python app.py）
    app.run(debug=True, host='0.0.0.0', port=5000)