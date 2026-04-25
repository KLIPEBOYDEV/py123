from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from database import init_db, get_db
import hashlib
import os
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = 'dance_studio_secret_2024'

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def current_role():
    return session.get('role', 'admin')

def is_admin():
    return current_role() == 'admin'

def current_trainer_id():
    return session.get('trainer_id')

def sync_session_user():
    """Keep role/trainer binding consistent with DB for current session user."""
    if 'user_id' not in session:
        return
    db = get_db()
    user = db.execute('SELECT role, trainer_id, name FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if not user:
        session.clear()
        return
    session['role'] = user['role']
    session['trainer_id'] = user['trainer_id']
    session['user_name'] = user['name']

def trainer_in_group(db, group_id, trainer_id):
    if not group_id or not trainer_id:
        return False
    row = db.execute(
        'SELECT 1 FROM group_trainers WHERE group_id=? AND trainer_id=?',
        (group_id, trainer_id)
    ).fetchone()
    return bool(row)

def trainer_group_filter_sql():
    """SQL фрагмент, который проверяет принадлежность группы тренеру через group_trainers."""
    return ' AND EXISTS (SELECT 1 FROM group_trainers gt WHERE gt.group_id=g.id AND gt.trainer_id=?) '

def group_trainers_names_subquery(alias='g'):
    return f"(SELECT GROUP_CONCAT(t2.name, ', ') FROM group_trainers gt2 JOIN trainers t2 ON t2.id=gt2.trainer_id WHERE gt2.group_id={alias}.id)"

def _as_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, 'year') and hasattr(value, 'month') and hasattr(value, 'day'):
        return value
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except Exception:
        return None

def apply_calendar_charges_for_date(db, target_date):
    """Списание по календарю для абонементов Стандарт (carry_over=0).

    Правила:
    - списывать 1 занятие за каждое занятие по расписанию в этот день
    - не зависеть от отметки посещаемости
    - не списывать после end_date
    - не списывать больше lessons_total (для наших типов = 8)
    - не списывать повторно, если уже списали за эту тренировку (attendance.charged_lessons)
    """
    d = _as_date(target_date)
    if not d:
        return 0
    date_str = d.strftime('%Y-%m-%d')

    rows = db.execute('''
        SELECT
            s.id as schedule_id,
            s.group_id,
            cg.client_id
        FROM schedule s
        JOIN client_groups cg ON cg.group_id = s.group_id
        WHERE s.date = ?
        ORDER BY s.id
    ''', (date_str,)).fetchall()

    charged = 0
    for r in rows:
        schedule_id = r['schedule_id']
        group_id = r['group_id']
        client_id = r['client_id']

        att = db.execute('SELECT * FROM attendance WHERE schedule_id=? AND client_id=?', (schedule_id, client_id)).fetchone()
        if not att:
            db.execute(
                'INSERT INTO attendance (schedule_id, client_id, present, charged_lessons, charged_subscription_id) VALUES (?,?,?,?,?)',
                (schedule_id, client_id, 0, 0, None)
            )
            att = db.execute('SELECT * FROM attendance WHERE schedule_id=? AND client_id=?', (schedule_id, client_id)).fetchone()

        if att['charged_lessons'] == 1:
            continue

        sub = db.execute('''
            SELECT s.*
            FROM subscriptions s
            JOIN subscription_types st ON st.id = s.type_id
            WHERE s.client_id=?
            AND (s.group_id=? OR s.group_id IS NULL)
            AND s.status='active'
            AND s.lessons_left > 0
            AND s.end_date >= ?
            AND st.carry_over=0
            ORDER BY s.end_date
            LIMIT 1
        ''', (client_id, group_id, date_str)).fetchone()
        if not sub:
            continue

        if sub['lessons_left'] <= 0:
            continue
        if sub['lessons_left'] > sub['lessons_total']:
            new_left = sub['lessons_total']
            db.execute('UPDATE subscriptions SET lessons_left=? WHERE id=?', (new_left, sub['id']))
            sub = dict(sub)
            sub['lessons_left'] = new_left

        new_left = sub['lessons_left'] - 1
        if new_left < 0:
            new_left = 0
        new_status = 'used' if new_left <= 0 else 'active'
        db.execute('UPDATE subscriptions SET lessons_left=?, status=? WHERE id=?', (new_left, new_status, sub['id']))
        db.execute('UPDATE attendance SET charged_lessons=1, charged_subscription_id=? WHERE id=?', (sub['id'], att['id']))
        charged += 1

    return charged

def apply_calendar_charges_for_active_standard(db, max_days_back=365):
    """Досписание по календарю для Стандарт за весь период действия активного абонемента.

    Проходим по всем активным Стандарт абонементам и списываем за все занятия по расписанию
    в интервале [start_date, min(end_date, today)] которые ещё не списаны (attendance.charged_lessons=0).
    max_days_back — страховка от очень старых данных.
    """
    today = datetime.now().date()
    total_charged = 0

    subs = db.execute('''
        SELECT s.*, st.carry_over
        FROM subscriptions s
        JOIN subscription_types st ON st.id = s.type_id
        WHERE s.status='active'
        AND s.lessons_left > 0
        AND st.carry_over = 0
    ''').fetchall()

    for sub in subs:
        if sub['lessons_left'] <= 0:
            continue

        start_d = _as_date(sub['start_date'])
        end_d = _as_date(sub['end_date'])
        if not start_d or not end_d:
            continue
        if end_d < start_d:
            continue

        effective_end = min(end_d, today)
        # страховка от слишком длинного диапазона
        min_start = today - timedelta(days=max_days_back)
        effective_start = max(start_d, min_start)
        if effective_end < effective_start:
            continue

        group_id = sub['group_id']
        if not group_id:
            # у Стандарта по описанию "только одна группа"; если нет группы — не знаем расписание
            continue

        # Сколько нужно списать максимум
        remaining_cap = sub['lessons_left']
        if remaining_cap > sub['lessons_total']:
            remaining_cap = sub['lessons_total']

        # Найдём все тренировки этой группы в диапазоне
        sessions = db.execute('''
            SELECT id, date
            FROM schedule
            WHERE group_id=? AND date >= ? AND date <= ?
            ORDER BY date, time
        ''', (group_id, effective_start.strftime('%Y-%m-%d'), effective_end.strftime('%Y-%m-%d'))).fetchall()

        for sess in sessions:
            if remaining_cap <= 0:
                break

            # гарантируем строку attendance
            att = db.execute(
                'SELECT * FROM attendance WHERE schedule_id=? AND client_id=?',
                (sess['id'], sub['client_id'])
            ).fetchone()
            if not att:
                db.execute(
                    'INSERT INTO attendance (schedule_id, client_id, present, charged_lessons, charged_subscription_id) VALUES (?,?,?,?,?)',
                    (sess['id'], sub['client_id'], 0, 0, None)
                )
                att = db.execute(
                    'SELECT * FROM attendance WHERE schedule_id=? AND client_id=?',
                    (sess['id'], sub['client_id'])
                ).fetchone()

            if att['charged_lessons'] == 1:
                continue

            # Списываем 1 занятие
            new_left = sub['lessons_left'] - 1
            if new_left < 0:
                new_left = 0
            new_status = 'used' if new_left <= 0 else 'active'
            db.execute('UPDATE subscriptions SET lessons_left=?, status=? WHERE id=?', (new_left, new_status, sub['id']))
            db.execute('UPDATE attendance SET charged_lessons=1, charged_subscription_id=? WHERE id=?', (sub['id'], att['id']))

            # обновляем локальные значения
            sub = dict(sub)
            sub['lessons_left'] = new_left
            remaining_cap -= 1
            total_charged += 1

    return total_charged

@app.before_request
def setup():
    if request.endpoint is None:
        return
    if request.endpoint in ('login', 'logout', 'static'):
        return
    if 'user_id' not in session:
        return
    sync_session_user()
    if is_admin():
        return

    trainer_allowed = {
        'dashboard', 'schedule', 'add_schedule', 'add_recurring_schedule',
        'attendance_form', 'mark_attendance', 'add_intensive',
        'edit_intensive', 'delete_intensive', 'logout'
    }
    if request.endpoint not in trainer_allowed:
        flash('Недостаточно прав для этого раздела', 'warning')
        return redirect(url_for('schedule'))

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        if not is_admin():
            flash('Доступно только администратору', 'error')
            return redirect(url_for('schedule'))
        return f(*args, **kwargs)
    return decorated

# ─── AUTH ────────────────────────────────────────────────────────────────────

@app.route('/', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('schedule') if current_role() == 'trainer' else url_for('dashboard'))
    if request.method == 'POST':
        db = get_db()
        username = request.form['username']
        password = hash_password(request.form['password'])
        user = db.execute('SELECT * FROM users WHERE username=? AND password=?', (username, password)).fetchone()
        if user:
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            session['role'] = user['role']
            session['trainer_id'] = user['trainer_id']
            return redirect(url_for('schedule') if user['role'] == 'trainer' else url_for('dashboard'))
        flash('Неверный логин или пароль', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─── DASHBOARD ───────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    if current_role() == 'trainer':
        today = datetime.now().strftime('%Y-%m-%d')
        schedule_today = db.execute('''
            SELECT s.*, g.name as group_name,
                   {trainers_names} as trainer_name
            FROM schedule s
            JOIN groups g ON s.group_id = g.id
            WHERE s.date = ? {trainer_filter}
            ORDER BY s.time
        '''.format(trainers_names=group_trainers_names_subquery('g'), trainer_filter=trainer_group_filter_sql()), (today, current_trainer_id())).fetchall()
        stats = {
            'clients': db.execute('''SELECT COUNT(DISTINCT cg.client_id) as c
                                     FROM client_groups cg
                                     JOIN groups g ON g.id=cg.group_id
                                     WHERE EXISTS (SELECT 1 FROM group_trainers gt WHERE gt.group_id=g.id AND gt.trainer_id=?)''', (current_trainer_id(),)).fetchone()['c'],
            'groups': db.execute('SELECT COUNT(DISTINCT g.id) as c FROM groups g JOIN group_trainers gt ON gt.group_id=g.id WHERE gt.trainer_id=?', (current_trainer_id(),)).fetchone()['c'],
            'trainers': 1,
            'subscriptions': db.execute('''SELECT COUNT(*) as c FROM subscriptions s
                                           JOIN groups g ON g.id=s.group_id
                                           WHERE EXISTS (SELECT 1 FROM group_trainers gt WHERE gt.group_id=g.id AND gt.trainer_id=?)
                                           AND s.status='active' ''', (current_trainer_id(),)).fetchone()['c'],
        }
        return render_template('dashboard.html', stats=stats, schedule_today=schedule_today, today=today)

    stats = {
        'clients': db.execute('SELECT COUNT(*) as c FROM clients').fetchone()['c'],
        'groups': db.execute('SELECT COUNT(*) as c FROM groups').fetchone()['c'],
        'trainers': db.execute('SELECT COUNT(*) as c FROM trainers').fetchone()['c'],
        'subscriptions': db.execute("SELECT COUNT(*) as c FROM subscriptions WHERE status='active'").fetchone()['c'],
    }
    today = datetime.now().strftime('%Y-%m-%d')
    schedule_today = db.execute('''
        SELECT s.*, g.name as group_name,
               {trainers_names} as trainer_name
        FROM schedule s
        JOIN groups g ON s.group_id = g.id
        WHERE s.date = ? ORDER BY s.time
    '''.format(trainers_names=group_trainers_names_subquery('g')), (today,)).fetchall()
    return render_template('dashboard.html', stats=stats, schedule_today=schedule_today, today=today)

@app.route('/analytics')
@login_required
def analytics():
    if not is_admin():
        flash('Аналитика доступна только администратору', 'error')
        return redirect(url_for('schedule'))
    db = get_db()
    today = datetime.now().date()
    default_from = today.replace(day=1).strftime('%Y-%m-%d')
    default_to = today.strftime('%Y-%m-%d')

    date_from = request.args.get('date_from', default_from)
    date_to = request.args.get('date_to', default_to)

    try:
        from_obj = datetime.strptime(date_from, '%Y-%m-%d').date()
        to_obj = datetime.strptime(date_to, '%Y-%m-%d').date()
    except ValueError:
        flash('Неверный формат дат фильтра', 'error')
        return redirect(url_for('analytics'))

    if to_obj < from_obj:
        flash('Дата "по" не может быть раньше даты "с"', 'error')
        return redirect(url_for('analytics', date_from=default_from, date_to=default_to))

    attendance_stats = db.execute('''
        SELECT COUNT(*) as total_marks, SUM(CASE WHEN a.present=1 THEN 1 ELSE 0 END) as present_marks
        FROM attendance a
        JOIN schedule s ON s.id = a.schedule_id
        WHERE s.date BETWEEN ? AND ?
    ''', (date_from, date_to)).fetchone()

    revenue_stats = db.execute('''
        SELECT
            COALESCE(SUM(
                CASE
                    WHEN payment_method='cash' THEN CASE WHEN COALESCE(cash_amount, 0) > 0 THEN cash_amount ELSE price_paid END
                    WHEN payment_method='mixed' THEN COALESCE(cash_amount, 0)
                    ELSE 0
                END
            ), 0) as cash_total,
            COALESCE(SUM(
                CASE
                    WHEN payment_method='card' THEN CASE WHEN COALESCE(non_cash_amount, 0) > 0 THEN non_cash_amount ELSE price_paid END
                    WHEN payment_method='mixed' THEN COALESCE(non_cash_amount, 0)
                    ELSE 0
                END
            ), 0) as card_total,
            COALESCE(SUM(price_paid), 0) as all_total
        FROM subscriptions
        WHERE created_at >= ? AND created_at < date(?, '+1 day')
        AND status != 'cancelled'
    ''', (date_from, date_to)).fetchone()

    cancelled_stats = {'cancelled_count': 0, 'cancelled_amount': 0}

    intensive_money = db.execute('''
        SELECT
            COALESCE(SUM(
                CASE
                    WHEN ic.payment_method='cash' THEN CASE WHEN COALESCE(ic.cash_amount, 0) > 0 THEN ic.cash_amount ELSE ic.amount END
                    WHEN ic.payment_method='mixed' THEN COALESCE(ic.cash_amount, 0)
                    ELSE 0
                END
            ), 0) as cash_total,
            COALESCE(SUM(
                CASE
                    WHEN ic.payment_method='card' THEN CASE WHEN COALESCE(ic.non_cash_amount, 0) > 0 THEN ic.non_cash_amount ELSE ic.amount END
                    WHEN ic.payment_method='mixed' THEN COALESCE(ic.non_cash_amount, 0)
                    ELSE 0
                END
            ), 0) as card_total
        FROM intensive_clients ic
        JOIN intensives i ON i.id = ic.intensive_id
        WHERE i.created_at >= ? AND i.created_at < date(?, '+1 day')
        AND ic.payment_type = 'cash'
    ''', (date_from, date_to)).fetchone()

    trainer_stats = db.execute('''
        SELECT
            t.id,
            t.name as trainer_name,
            (
                SELECT COUNT(*)
                FROM schedule s
                JOIN groups g2 ON g2.id = s.group_id
                WHERE EXISTS (SELECT 1 FROM group_trainers gt WHERE gt.group_id=g2.id AND gt.trainer_id=t.id)
                AND s.date BETWEEN ? AND ?
            ) as trainings_count,
            (
                SELECT COUNT(*)
                FROM attendance a
                JOIN schedule s ON s.id = a.schedule_id
                JOIN groups g2 ON g2.id = s.group_id
                WHERE EXISTS (SELECT 1 FROM group_trainers gt WHERE gt.group_id=g2.id AND gt.trainer_id=t.id)
                AND a.present=1 AND s.date BETWEEN ? AND ?
            ) as attendance_count,
            (
                SELECT COUNT(DISTINCT cg.client_id)
                FROM client_groups cg
                JOIN groups g2 ON g2.id = cg.group_id
                WHERE EXISTS (SELECT 1 FROM group_trainers gt WHERE gt.group_id=g2.id AND gt.trainer_id=t.id)
            ) as students_count
            ,(
                SELECT COUNT(*)
                FROM intensives i
                WHERE i.trainer_id = t.id
                AND i.date BETWEEN ? AND ?
            ) as intensives_count
            ,(
                SELECT COALESCE(SUM(i.hours), 0)
                FROM intensives i
                WHERE i.trainer_id = t.id
                AND i.date BETWEEN ? AND ?
            ) as intensive_hours
        FROM trainers t
        ORDER BY trainings_count DESC, t.name COLLATE NOCASE
    ''', (date_from, date_to, date_from, date_to, date_from, date_to, date_from, date_to)).fetchall()

    group_stats = db.execute('''
        SELECT
            g.id,
            g.name as group_name,
            COALESCE({trainers_names}, '—') as trainer_name,
            (
                SELECT COUNT(*)
                FROM schedule s
                WHERE s.group_id = g.id AND s.date BETWEEN ? AND ?
            ) as trainings_count,
            (
                SELECT COUNT(DISTINCT cg.client_id)
                FROM client_groups cg
                WHERE cg.group_id = g.id
            ) as students_count,
            (
                SELECT COUNT(*)
                FROM attendance a
                JOIN schedule s ON s.id = a.schedule_id
                WHERE s.group_id = g.id AND a.present=1 AND s.date BETWEEN ? AND ?
            ) as attendance_count,
            (
                SELECT COALESCE(SUM(sub.price_paid), 0)
                FROM subscriptions sub
                WHERE sub.group_id = g.id
                AND sub.created_at >= ?
                AND sub.created_at < date(?, '+1 day')
                AND sub.status != 'cancelled'
            ) as revenue
            ,(
                SELECT COUNT(*)
                FROM intensives i
                WHERE i.group_id = g.id
                AND i.date BETWEEN ? AND ?
            ) as intensives_count
            ,(
                SELECT COALESCE(SUM(i.hours), 0)
                FROM intensives i
                WHERE i.group_id = g.id
                AND i.date BETWEEN ? AND ?
            ) as intensive_hours
        FROM groups g
        ORDER BY revenue DESC, g.name COLLATE NOCASE
    '''.format(trainers_names=group_trainers_names_subquery('g')), (date_from, date_to, date_from, date_to, date_from, date_to, date_from, date_to, date_from, date_to)).fetchall()

    present_marks = attendance_stats['present_marks'] or 0
    total_marks = attendance_stats['total_marks'] or 0
    attendance_percent = round((present_marks / total_marks) * 100, 1) if total_marks else 0

    cash_total = revenue_stats['cash_total'] + intensive_money['cash_total']
    card_total = revenue_stats['card_total'] + intensive_money['card_total']
    all_total = cash_total + card_total

    return render_template(
        'analytics.html',
        date_from=date_from,
        date_to=date_to,
        present_marks=present_marks,
        total_marks=total_marks,
        attendance_percent=attendance_percent,
        cash_total=cash_total,
        card_total=card_total,
        all_total=all_total,
        cancelled_count=cancelled_stats['cancelled_count'],
        cancelled_amount=cancelled_stats['cancelled_amount'],
        trainer_stats=trainer_stats,
        group_stats=group_stats
    )

@app.route('/analytics/operations')
@login_required
def analytics_operations():
    if not is_admin():
        flash('Раздел доступен только администратору', 'error')
        return redirect(url_for('schedule'))
    db = get_db()
    today = datetime.now().date()
    default_from = today.replace(day=1).strftime('%Y-%m-%d')
    default_to = today.strftime('%Y-%m-%d')
    date_from = request.args.get('date_from', default_from)
    date_to = request.args.get('date_to', default_to)

    operations = db.execute('''
        SELECT * FROM (
            SELECT
                s.created_at as operation_date,
                'income' as operation_kind,
                'Абонемент' as source,
                c.name as client_name,
                COALESCE(g.name, '—') as group_name,
                st.name as details,
                s.payment_method as payment_method,
                s.price_paid as amount
            FROM subscriptions s
            JOIN clients c ON c.id = s.client_id
            JOIN subscription_types st ON st.id = s.type_id
            LEFT JOIN groups g ON g.id = s.group_id
            WHERE s.created_at >= ? AND s.created_at < date(?, '+1 day')
            AND s.status != 'cancelled'

            UNION ALL

            SELECT
                i.created_at as operation_date,
                CASE WHEN ic.payment_type='cash' THEN 'income' ELSE 'writeoff' END as operation_kind,
                'Интенсив' as source,
                c.name as client_name,
                COALESCE(g.name, '—') as group_name,
                CASE
                    WHEN ic.payment_type='cash' THEN 'Оплата интенсива (' || ic.hours || ' ч)'
                    ELSE 'Списание абонемента за интенсив (' || ic.lessons_written_off || ' зан.)'
                END as details,
                CASE
                    WHEN ic.payment_type='cash' THEN ic.payment_method
                    ELSE 'subscription'
                END as payment_method,
                CASE
                    WHEN ic.payment_type='cash' THEN ic.amount
                    ELSE 0
                END as amount
            FROM intensive_clients ic
            JOIN intensives i ON i.id = ic.intensive_id
            JOIN clients c ON c.id = ic.client_id
            LEFT JOIN groups g ON g.id = i.group_id
            WHERE i.created_at >= ? AND i.created_at < date(?, '+1 day')
        )
        ORDER BY operation_date DESC
    ''', (date_from, date_to, date_from, date_to)).fetchall()

    return render_template('analytics_operations.html', operations=operations, date_from=date_from, date_to=date_to)

@app.route('/accounts')
@login_required
@admin_required
def accounts():
    db = get_db()
    users = db.execute('''
        SELECT u.*, t.name as trainer_name
        FROM users u
        LEFT JOIN trainers t ON t.id = u.trainer_id
        ORDER BY CASE WHEN u.role='admin' THEN 0 ELSE 1 END, u.name COLLATE NOCASE
    ''').fetchall()
    return render_template('accounts.html', users=users)

@app.route('/accounts/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_account(id):
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (id,)).fetchone()
    if not user:
        flash('Пользователь не найден', 'error')
        return redirect(url_for('accounts'))
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form.get('password', '').strip()
        existing = db.execute('SELECT id FROM users WHERE username=? AND id!=?', (username, id)).fetchone()
        if existing:
            flash('Такой логин уже используется', 'error')
            return render_template('account_form.html', user=user)
        if password:
            db.execute('UPDATE users SET username=?, password=? WHERE id=?', (username, hash_password(password), id))
        else:
            db.execute('UPDATE users SET username=? WHERE id=?', (username, id))
        db.commit()
        flash('Данные аккаунта сохранены', 'success')
        return redirect(url_for('accounts'))
    return render_template('account_form.html', user=user)

# ─── SUBSCRIPTION TYPES ──────────────────────────────────────────────────────

@app.route('/subscription-types')
@login_required
def subscription_types():
    db = get_db()
    types = db.execute('SELECT * FROM subscription_types ORDER BY id').fetchall()
    return render_template('subscription_types.html', types=types)

@app.route('/subscription-types/add', methods=['GET', 'POST'])
@login_required
def add_subscription_type():
    if request.method == 'POST':
        db = get_db()
        db.execute('''INSERT INTO subscription_types 
            (name, lessons_count, price, validity_days, carry_over, description)
            VALUES (?,?,?,?,?,?)''', (
            request.form['name'],
            int(request.form['lessons_count']),
            float(request.form['price']),
            int(request.form['validity_days']),
            1 if request.form.get('carry_over') else 0,
            request.form.get('description', '')
        ))
        db.commit()
        flash('Тип абонемента добавлен', 'success')
        return redirect(url_for('subscription_types'))
    return render_template('subscription_type_form.html', type=None)

@app.route('/subscription-types/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_subscription_type(id):
    db = get_db()
    t = db.execute('SELECT * FROM subscription_types WHERE id=?', (id,)).fetchone()
    if request.method == 'POST':
        db.execute('''UPDATE subscription_types SET name=?, lessons_count=?, price=?, 
            validity_days=?, carry_over=?, description=? WHERE id=?''', (
            request.form['name'],
            int(request.form['lessons_count']),
            float(request.form['price']),
            int(request.form['validity_days']),
            1 if request.form.get('carry_over') else 0,
            request.form.get('description', ''),
            id
        ))
        db.commit()
        flash('Тип абонемента обновлён', 'success')
        return redirect(url_for('subscription_types'))
    return render_template('subscription_type_form.html', type=t)

@app.route('/subscription-types/delete/<int:id>', methods=['POST'])
@login_required
def delete_subscription_type(id):
    db = get_db()
    db.execute('DELETE FROM subscription_types WHERE id=?', (id,))
    db.commit()
    flash('Тип абонемента удалён', 'success')
    return redirect(url_for('subscription_types'))

# ─── TRAINERS ────────────────────────────────────────────────────────────────

@app.route('/trainers')
@login_required
def trainers():
    db = get_db()
    if not is_admin():
        flash('Раздел тренеров доступен только администратору', 'error')
        return redirect(url_for('schedule'))
    trainers_list = db.execute('''
        SELECT t.*, COUNT(g.id) as groups_count 
        FROM trainers t LEFT JOIN groups g ON t.id = g.trainer_id
        GROUP BY t.id ORDER BY t.name COLLATE NOCASE
    ''').fetchall()
    return render_template('trainers.html', trainers=trainers_list)

@app.route('/trainers/add', methods=['GET', 'POST'])
@login_required
def add_trainer():
    if not is_admin():
        flash('Доступно только администратору', 'error')
        return redirect(url_for('trainers'))
    if request.method == 'POST':
        db = get_db()
        role = request.form.get('role', 'trainer')
        if role not in ('trainer', 'admin'):
            role = 'trainer'
        name = request.form['name'].strip()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if not username:
            flash('Укажите логин для входа', 'error')
            return render_template('trainer_form.html', trainer=None, selected_role=role)
        existing_user = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if existing_user:
            flash('Такой логин уже используется', 'error')
            return render_template('trainer_form.html', trainer=None, selected_role=role)
        cursor = db.execute('INSERT INTO trainers (name, phone, email, specialization, notes) VALUES (?,?,?,?,?)', (
            name, request.form.get('phone',''),
            request.form.get('email',''), request.form.get('specialization',''),
            request.form.get('notes','')
        ))
        trainer_id = cursor.lastrowid
        if not password:
            password = 'trainer123' if role == 'trainer' else 'admin123'
        trainer_binding = trainer_id if role == 'trainer' else None
        db.execute(
            "INSERT INTO users (role, username, password, name, trainer_id) VALUES (?, ?, ?, ?, ?)",
            (role, username, hash_password(password), name, trainer_binding)
        )
        db.commit()
        role_label = 'Тренер' if role == 'trainer' else 'Администратор'
        flash(f"{role_label} добавлен (логин: {username})", 'success')
        return redirect(url_for('trainers'))
    return render_template('trainer_form.html', trainer=None, selected_role='trainer')

@app.route('/trainers/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_trainer(id):
    db = get_db()
    trainer = db.execute('SELECT * FROM trainers WHERE id=?', (id,)).fetchone()
    trainer_account = db.execute(
        "SELECT * FROM users WHERE role IN ('trainer','admin') AND trainer_id=?",
        (id,)
    ).fetchone()
    if request.method == 'POST':
        if not is_admin():
            flash('Редактирование тренеров доступно только администратору', 'error')
            return redirect(url_for('trainers'))
        # Менять роль может только администратор
        if is_admin() and trainer_account:
            new_role = request.form.get('role', trainer_account['role'])
            if new_role not in ('trainer', 'admin'):
                new_role = trainer_account['role']
            # если делаем администратором — отвязываем trainer_id, если тренером — привязываем
            new_trainer_binding = id if new_role == 'trainer' else None
            db.execute(
                'UPDATE users SET role=?, trainer_id=? WHERE id=?',
                (new_role, new_trainer_binding, trainer_account['id'])
            )
        db.execute('UPDATE trainers SET name=?, phone=?, email=?, specialization=?, notes=? WHERE id=?', (
            request.form['name'], request.form.get('phone',''),
            request.form.get('email',''), request.form.get('specialization',''),
            request.form.get('notes',''), id
        ))
        # обновляем имя аккаунта (и для trainer, и для admin если вдруг привязан)
        if trainer_account:
            db.execute('UPDATE users SET name=? WHERE id=?', (request.form['name'], trainer_account['id']))
        db.commit()
        flash('Данные тренера обновлены', 'success')
        return redirect(url_for('trainers'))
    selected_role = trainer_account['role'] if trainer_account else 'trainer'
    return render_template('trainer_form.html', trainer=trainer, trainer_account=trainer_account, selected_role=selected_role, is_admin=is_admin())

@app.route('/trainers/delete/<int:id>', methods=['POST'])
@login_required
def delete_trainer(id):
    if not is_admin():
        flash('Доступно только администратору', 'error')
        return redirect(url_for('trainers'))
    db = get_db()
    db.execute('DELETE FROM trainers WHERE id=?', (id,))
    db.commit()
    flash('Тренер удалён', 'success')
    return redirect(url_for('trainers'))

# ─── GROUPS ──────────────────────────────────────────────────────────────────

@app.route('/groups')
@login_required
def groups():
    db = get_db()
    groups_list = db.execute('''
        SELECT g.*,
               {trainers_names} as trainer_name,
               COUNT(cg.client_id) as clients_count
        FROM groups g
        LEFT JOIN client_groups cg ON g.id = cg.group_id
        GROUP BY g.id ORDER BY g.name COLLATE NOCASE
    '''.format(trainers_names=group_trainers_names_subquery('g'))).fetchall()
    return render_template('groups.html', groups=groups_list)

@app.route('/groups/add', methods=['GET', 'POST'])
@login_required
def add_group():
    db = get_db()
    trainers_list = db.execute('SELECT * FROM trainers ORDER BY name COLLATE NOCASE').fetchall()
    if request.method == 'POST':
        db.execute('INSERT INTO groups (name, trainer_id, age_range, level, max_capacity, description) VALUES (?,?,?,?,?,?)', (
            request.form['name'], request.form.get('trainer_id') or None,
            request.form.get('age_range',''), request.form.get('level',''),
            int(request.form.get('max_capacity', 20)), request.form.get('description','')
        ))
        gid = db.execute('SELECT last_insert_rowid() as id').fetchone()['id']
        selected_trainers = [int(x) for x in request.form.getlist('trainer_ids') if str(x).strip().isdigit()]
        main_tid = request.form.get('trainer_id')
        if main_tid and str(main_tid).strip().isdigit():
            selected_trainers.append(int(main_tid))
        selected_trainers = sorted(set(selected_trainers))
        if len(selected_trainers) > 2:
            flash('В группе можно выбрать максимум 2 тренера', 'error')
            db.rollback()
            return render_template('group_form.html', group=None, trainers=trainers_list, selected_trainer_ids=selected_trainers)
        for tid in sorted(set(selected_trainers)):
            db.execute('INSERT OR IGNORE INTO group_trainers (group_id, trainer_id) VALUES (?,?)', (gid, tid))
        db.commit()
        flash('Группа создана', 'success')
        return redirect(url_for('groups'))
    return render_template('group_form.html', group=None, trainers=trainers_list)

@app.route('/groups/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_group(id):
    db = get_db()
    group = db.execute('SELECT * FROM groups WHERE id=?', (id,)).fetchone()
    trainers_list = db.execute('SELECT * FROM trainers ORDER BY name COLLATE NOCASE').fetchall()
    selected_trainer_ids = [r['trainer_id'] for r in db.execute('SELECT trainer_id FROM group_trainers WHERE group_id=?', (id,)).fetchall()]
    if request.method == 'POST':
        db.execute('UPDATE groups SET name=?, trainer_id=?, age_range=?, level=?, max_capacity=?, description=? WHERE id=?', (
            request.form['name'], request.form.get('trainer_id') or None,
            request.form.get('age_range',''), request.form.get('level',''),
            int(request.form.get('max_capacity', 20)), request.form.get('description',''), id
        ))
        db.execute('DELETE FROM group_trainers WHERE group_id=?', (id,))
        selected_trainers = [int(x) for x in request.form.getlist('trainer_ids') if str(x).strip().isdigit()]
        main_tid = request.form.get('trainer_id')
        if main_tid and str(main_tid).strip().isdigit():
            selected_trainers.append(int(main_tid))
        selected_trainers = sorted(set(selected_trainers))
        if len(selected_trainers) > 2:
            flash('В группе можно выбрать максимум 2 тренера', 'error')
            db.rollback()
            return render_template('group_form.html', group=group, trainers=trainers_list, selected_trainer_ids=selected_trainers)
        for tid in sorted(set(selected_trainers)):
            db.execute('INSERT OR IGNORE INTO group_trainers (group_id, trainer_id) VALUES (?,?)', (id, tid))
        db.commit()
        flash('Группа обновлена', 'success')
        return redirect(url_for('groups'))
    return render_template('group_form.html', group=group, trainers=trainers_list, selected_trainer_ids=selected_trainer_ids)

@app.route('/groups/delete/<int:id>', methods=['POST'])
@login_required
def delete_group(id):
    db = get_db()
    db.execute('DELETE FROM groups WHERE id=?', (id,))
    db.commit()
    flash('Группа удалена', 'success')
    return redirect(url_for('groups'))

@app.route('/groups/<int:id>')
@login_required
def group_detail(id):
    db = get_db()
    group = db.execute('''
        SELECT g.*, {trainers_names} as trainer_name
        FROM groups g
        WHERE g.id=?
    '''.format(trainers_names=group_trainers_names_subquery('g')), (id,)).fetchone()
    members = db.execute('''
        SELECT c.*, cg.joined_date FROM clients c
        JOIN client_groups cg ON c.id = cg.client_id
        WHERE cg.group_id=? ORDER BY CASE WHEN instr(c.name, ' ') > 0 THEN substr(c.name, 1, instr(c.name, ' ')-1) ELSE c.name END COLLATE NOCASE, c.name COLLATE NOCASE
    ''', (id,)).fetchall()
    all_clients = db.execute('''
        SELECT * FROM clients WHERE id NOT IN 
        (SELECT client_id FROM client_groups WHERE group_id=?) ORDER BY CASE WHEN instr(name, ' ') > 0 THEN substr(name, 1, instr(name, ' ')-1) ELSE name END COLLATE NOCASE, name COLLATE NOCASE
    ''', (id,)).fetchall()
    schedule = db.execute('SELECT * FROM schedule WHERE group_id=? ORDER BY date, time', (id,)).fetchall()
    return render_template('group_detail.html', group=group, members=members, all_clients=all_clients, schedule=schedule)

@app.route('/groups/<int:id>/add-client', methods=['POST'])
@login_required
def add_client_to_group(id):
    db = get_db()
    client_id = request.form['client_id']
    existing = db.execute('SELECT * FROM client_groups WHERE group_id=? AND client_id=?', (id, client_id)).fetchone()
    if not existing:
        db.execute('INSERT INTO client_groups (group_id, client_id, joined_date) VALUES (?,?,?)', 
                   (id, client_id, datetime.now().strftime('%Y-%m-%d')))
        db.commit()
        flash('Клиент добавлен в группу', 'success')
    else:
        flash('Клиент уже в этой группе', 'warning')
    return redirect(url_for('group_detail', id=id))

@app.route('/groups/<int:group_id>/remove-client/<int:client_id>', methods=['POST'])
@login_required
def remove_client_from_group(group_id, client_id):
    db = get_db()
    db.execute('DELETE FROM client_groups WHERE group_id=? AND client_id=?', (group_id, client_id))
    db.commit()
    flash('Клиент удалён из группы', 'success')
    return redirect(url_for('group_detail', id=group_id))

# ─── CLIENTS ─────────────────────────────────────────────────────────────────

@app.route('/clients')
@login_required
def clients():
    db = get_db()
    search = request.args.get('search', '')
    if search:
        clients_list = db.execute('''
            SELECT c.*, p.name as parent_name FROM clients c
            LEFT JOIN clients p ON c.parent_id = p.id
            WHERE c.name LIKE ? COLLATE NOCASE OR c.phone LIKE ? ORDER BY CASE WHEN instr(c.name, ' ') > 0 THEN substr(c.name, 1, instr(c.name, ' ')-1) ELSE c.name END COLLATE NOCASE, c.name COLLATE NOCASE
        ''', (f'%{search}%', f'%{search}%')).fetchall()
    else:
        clients_list = db.execute('''
            SELECT c.*, p.name as parent_name FROM clients c
            LEFT JOIN clients p ON c.parent_id = p.id
            ORDER BY CASE WHEN instr(c.name, ' ') > 0 THEN substr(c.name, 1, instr(c.name, ' ')-1) ELSE c.name END COLLATE NOCASE, c.name COLLATE NOCASE
        ''').fetchall()
    return render_template('clients.html', clients=clients_list, search=search)

@app.route('/clients/add', methods=['GET', 'POST'])
@login_required
def add_client():
    db = get_db()
    all_clients = db.execute("SELECT * FROM clients ORDER BY CASE WHEN instr(name, ' ') > 0 THEN substr(name, 1, instr(name, ' ')-1) ELSE name END COLLATE NOCASE, name COLLATE NOCASE").fetchall()
    if request.method == 'POST':
        parent_id = request.form.get('parent_id') or None
        if parent_id:
            existing = db.execute('SELECT id FROM clients WHERE id=?', (parent_id,)).fetchone()
            if not existing:
                parent_id = None
        db.execute('INSERT INTO clients (name, phone, email, birthdate, parent_id, notes) VALUES (?,?,?,?,?,?)', (
            request.form['name'], request.form.get('phone',''),
            request.form.get('email',''), request.form.get('birthdate',''),
            parent_id, request.form.get('notes','')
        ))
        db.commit()
        flash('Клиент добавлен', 'success')
        return redirect(url_for('clients'))
    return render_template('client_form.html', client=None, all_clients=all_clients, client_id=None)

@app.route('/clients/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_client(id):
    db = get_db()
    client = db.execute('SELECT * FROM clients WHERE id=?', (id,)).fetchone()
    all_clients = db.execute("SELECT * FROM clients WHERE id != ? ORDER BY CASE WHEN instr(name, ' ') > 0 THEN substr(name, 1, instr(name, ' ')-1) ELSE name END COLLATE NOCASE, name COLLATE NOCASE", (id,)).fetchall()
    if request.method == 'POST':
        parent_id = request.form.get('parent_id') or None
        if parent_id:
            existing = db.execute('SELECT id FROM clients WHERE id=?', (parent_id,)).fetchone()
            if not existing:
                parent_id = None
        if parent_id and int(parent_id) == id:
            parent_id = None
        db.execute('UPDATE clients SET name=?, phone=?, email=?, birthdate=?, parent_id=?, notes=? WHERE id=?', (
            request.form['name'], request.form.get('phone',''),
            request.form.get('email',''), request.form.get('birthdate',''),
            parent_id, request.form.get('notes',''), id
        ))
        db.commit()
        flash('Данные клиента обновлены', 'success')
        return redirect(url_for('clients'))
    return render_template('client_form.html', client=client, all_clients=all_clients, client_id=id)

@app.route('/clients/delete/<int:id>', methods=['POST'])
@login_required
def delete_client(id):
    db = get_db()
    db.execute('DELETE FROM clients WHERE id=?', (id,))
    db.commit()
    flash('Клиент удалён', 'success')
    return redirect(url_for('clients'))

@app.route('/api/clients/search')
@login_required
def search_clients():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
    db = get_db()
    clients = db.execute('''SELECT id, name FROM clients WHERE name LIKE ? COLLATE NOCASE ORDER BY CASE WHEN instr(name, ' ') > 0 THEN substr(name, 1, instr(name, ' ')-1) ELSE name END COLLATE NOCASE, name COLLATE NOCASE LIMIT 10''', (f'%{query}%',)).fetchall()
    return jsonify([{'id': c['id'], 'name': c['name']} for c in clients])

@app.route('/clients/<int:id>')
@login_required
def client_detail(id):
    db = get_db()
    client = db.execute('''SELECT c.*, p.name as parent_name, p.phone as parent_phone 
        FROM clients c LEFT JOIN clients p ON c.parent_id=p.id WHERE c.id=?''', (id,)).fetchone()
    children = db.execute('SELECT * FROM clients WHERE parent_id=?', (id,)).fetchall()
    groups = db.execute('''
        SELECT g.*, {trainers_names} as trainer_name
        FROM groups g
        JOIN client_groups cg ON g.id=cg.group_id
        WHERE cg.client_id=?
    '''.format(trainers_names=group_trainers_names_subquery('g')), (id,)).fetchall()
    subscriptions = db.execute('''SELECT s.*, st.name as type_name, g.name as group_name
        FROM subscriptions s
        JOIN subscription_types st ON s.type_id=st.id
        LEFT JOIN groups g ON s.group_id=g.id
        WHERE s.client_id=? AND s.status != 'cancelled'
        ORDER BY s.created_at DESC''', (id,)).fetchall()
    cancelled_subscriptions = db.execute('''SELECT s.*, st.name as type_name, g.name as group_name
        FROM subscriptions s
        JOIN subscription_types st ON s.type_id=st.id
        LEFT JOIN groups g ON s.group_id=g.id
        WHERE s.client_id=? AND s.status='cancelled'
        ORDER BY s.created_at DESC''', (id,)).fetchall()
    sub_types = db.execute('SELECT * FROM subscription_types').fetchall()
    groups_all = db.execute('SELECT * FROM groups ORDER BY name COLLATE NOCASE').fetchall()
    return render_template('client_detail.html', client=client, children=children,
                           groups=groups, subscriptions=subscriptions,
                           cancelled_subscriptions=cancelled_subscriptions,
                           sub_types=sub_types, groups_all=groups_all)

# ─── SUBSCRIPTIONS ───────────────────────────────────────────────────────────

@app.route('/subscriptions')
@login_required
def subscriptions():
    db = get_db()
    subs = db.execute('''SELECT s.*, c.name as client_name, st.name as type_name, g.name as group_name
        FROM subscriptions s
        JOIN clients c ON s.client_id=c.id
        JOIN subscription_types st ON s.type_id=st.id
        LEFT JOIN groups g ON s.group_id=g.id
        WHERE s.status != 'cancelled'
        ORDER BY s.created_at DESC''').fetchall()
    return render_template('subscriptions.html', subscriptions=subs)

@app.route('/subscriptions/add', methods=['GET', 'POST'])
@login_required
def add_subscription():
    db = get_db()
    clients_list = db.execute("SELECT * FROM clients ORDER BY CASE WHEN instr(name, ' ') > 0 THEN substr(name, 1, instr(name, ' ')-1) ELSE name END COLLATE NOCASE, name COLLATE NOCASE").fetchall()
    sub_types = db.execute('SELECT * FROM subscription_types').fetchall()
    groups_list = db.execute('SELECT * FROM groups ORDER BY name COLLATE NOCASE').fetchall()
    if request.method == 'POST':
        type_id = int(request.form['type_id'])
        stype = db.execute('SELECT * FROM subscription_types WHERE id=?', (type_id,)).fetchone()
        activation_date = request.form.get('activation_date') or request.form.get('start_date') or datetime.now().strftime('%Y-%m-%d')
        end_date = (datetime.strptime(activation_date, '%Y-%m-%d') + timedelta(days=stype['validity_days'])).strftime('%Y-%m-%d')
        status = 'active' if request.form.get('activate_now') else 'inactive'
        payment_method = request.form.get('payment_method', 'cash')
        price_paid = float(request.form.get('price_paid', stype['price']))
        raw_cash_amount = request.form.get('cash_amount')
        raw_non_cash_amount = request.form.get('non_cash_amount')

        if payment_method not in ('cash', 'card', 'mixed'):
            flash('Неверный способ оплаты', 'error')
            return redirect(request.url)
        if price_paid < 0:
            flash('Сумма оплаты не может быть отрицательной', 'error')
            return redirect(request.url)

        if payment_method == 'cash':
            cash_amount = round(price_paid, 2)
            non_cash_amount = 0.0
        elif payment_method == 'card':
            cash_amount = 0.0
            non_cash_amount = round(price_paid, 2)
        else:
            cash_amount = float(raw_cash_amount or 0)
            non_cash_amount = float(raw_non_cash_amount or 0)
            if cash_amount < 0 or non_cash_amount < 0:
                flash('Суммы оплаты не могут быть отрицательными', 'error')
                return redirect(request.url)
            if abs((cash_amount + non_cash_amount) - price_paid) > 0.01:
                flash('При разделении оплаты сумма наличных и безналичных должна равняться общей сумме', 'error')
                return redirect(request.url)
        db.execute('''INSERT INTO subscriptions 
            (client_id, type_id, group_id, lessons_total, lessons_left, start_date, end_date, price_paid, payment_method, cash_amount, non_cash_amount, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (
            request.form['client_id'],
            type_id,
            request.form.get('group_id') or None,
            stype['lessons_count'],
            stype['lessons_count'],
            activation_date, end_date,
            price_paid,
            payment_method,
            cash_amount,
            non_cash_amount,
            status
        ))
        db.commit()
        flash('Абонемент оформлен и активирован' if status == 'active' else 'Абонемент оформлен как неактивный', 'success')
        return redirect(url_for('subscriptions'))
    return render_template('subscription_form.html', clients=clients_list, sub_types=sub_types, groups=groups_list)

@app.route('/subscriptions/add-for-client/<int:client_id>', methods=['POST'])
@login_required
def add_subscription_for_client(client_id):
    db = get_db()
    type_id = int(request.form['type_id'])
    stype = db.execute('SELECT * FROM subscription_types WHERE id=?', (type_id,)).fetchone()
    activation_date = request.form.get('activation_date') or datetime.now().strftime('%Y-%m-%d')
    end_date = (datetime.strptime(activation_date, '%Y-%m-%d') + timedelta(days=stype['validity_days'])).strftime('%Y-%m-%d')
    status = 'active' if request.form.get('activate_now') else 'inactive'
    payment_method = request.form.get('payment_method', 'cash')
    price_paid = float(request.form.get('price_paid', stype['price']))
    raw_cash_amount = request.form.get('cash_amount')
    raw_non_cash_amount = request.form.get('non_cash_amount')

    if payment_method not in ('cash', 'card', 'mixed'):
        flash('Неверный способ оплаты', 'error')
        return redirect(url_for('client_detail', id=client_id))
    if price_paid < 0:
        flash('Сумма оплаты не может быть отрицательной', 'error')
        return redirect(url_for('client_detail', id=client_id))

    if payment_method == 'cash':
        cash_amount = round(price_paid, 2)
        non_cash_amount = 0.0
    elif payment_method == 'card':
        cash_amount = 0.0
        non_cash_amount = round(price_paid, 2)
    else:
        cash_amount = float(raw_cash_amount or 0)
        non_cash_amount = float(raw_non_cash_amount or 0)
        if cash_amount < 0 or non_cash_amount < 0:
            flash('Суммы оплаты не могут быть отрицательными', 'error')
            return redirect(url_for('client_detail', id=client_id))
        if abs((cash_amount + non_cash_amount) - price_paid) > 0.01:
            flash('При разделении оплаты сумма наличных и безналичных должна равняться общей сумме', 'error')
            return redirect(url_for('client_detail', id=client_id))
    db.execute('''INSERT INTO subscriptions 
        (client_id, type_id, group_id, lessons_total, lessons_left, start_date, end_date, price_paid, payment_method, cash_amount, non_cash_amount, status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (
        client_id, type_id,
        request.form.get('group_id') or None,
        stype['lessons_count'], stype['lessons_count'],
        activation_date, end_date, price_paid, payment_method, cash_amount, non_cash_amount, status
    ))
    db.commit()
    flash('Абонемент оформлен и активирован' if status == 'active' else 'Абонемент оформлен как неактивный', 'success')
    return redirect(url_for('client_detail', id=client_id))

@app.route('/subscriptions/cancel/<int:id>', methods=['POST'])
@login_required
def cancel_subscription(id):
    db = get_db()
    sub = db.execute('SELECT * FROM subscriptions WHERE id=?', (id,)).fetchone()
    if not sub:
        flash('Абонемент не найден', 'error')
        return redirect(url_for('subscriptions'))
    db.execute("UPDATE subscriptions SET status='cancelled' WHERE id=?", (id,))
    db.commit()
    flash('Абонемент аннулирован', 'success')
    return redirect(url_for('client_detail', id=sub['client_id']))

@app.route('/subscriptions/activate/<int:id>', methods=['POST'])
@login_required
def activate_subscription(id):
    db = get_db()
    sub = db.execute('SELECT * FROM subscriptions WHERE id=?', (id,)).fetchone()
    if not sub:
        flash('Абонемент не найден', 'error')
        return redirect(url_for('subscriptions'))
    activation_date = request.form.get('activation_date') or datetime.now().strftime('%Y-%m-%d')
    stype = db.execute('SELECT validity_days FROM subscription_types WHERE id=?', (sub['type_id'],)).fetchone()
    end_date = (datetime.strptime(activation_date, '%Y-%m-%d') + timedelta(days=stype['validity_days'])).strftime('%Y-%m-%d')
    if sub['lessons_left'] <= 0:
        flash('Нельзя активировать абонемент без оставшихся занятий', 'error')
    elif sub['status'] == 'cancelled':
        flash('Аннулированный абонемент нельзя активировать, только удалить', 'error')
    else:
        db.execute("UPDATE subscriptions SET status='active', start_date=?, end_date=? WHERE id=?", (activation_date, end_date, id))
        db.commit()
        flash('Абонемент активирован', 'success')
    return redirect(url_for('client_detail', id=sub['client_id']))

@app.route('/subscriptions/delete/<int:id>', methods=['POST'])
@login_required
def delete_subscription(id):
    db = get_db()
    sub = db.execute('SELECT * FROM subscriptions WHERE id=?', (id,)).fetchone()
    if not sub:
        flash('Абонемент не найден', 'error')
        return redirect(url_for('subscriptions'))
    if sub['status'] != 'cancelled':
        flash('Удалять можно только аннулированные абонементы', 'error')
        return redirect(url_for('client_detail', id=sub['client_id']))
    db.execute('DELETE FROM subscriptions WHERE id=?', (id,))
    db.commit()
    flash('Аннулированный абонемент удалён', 'success')
    return redirect(url_for('client_detail', id=sub['client_id']))

@app.route('/subscriptions/delete-cancelled', methods=['POST'])
@login_required
def delete_cancelled_subscriptions():
    db = get_db()
    deleted = db.execute("SELECT COUNT(*) as c FROM subscriptions WHERE status='cancelled'").fetchone()['c']
    db.execute("DELETE FROM subscriptions WHERE status='cancelled'")
    db.commit()
    flash(f'Удалено аннулированных абонементов: {deleted}', 'success')
    return redirect(url_for('subscriptions'))

# ─── SCHEDULE ────────────────────────────────────────────────────────────────

@app.route('/schedule')
@login_required
def schedule():
    db = get_db()
    # Календарное списание для "Стандарт" выполняем при заходе в расписание.
    # Это гарантирует, что остатки будут актуальны без отдельной кнопки.
    try:
        apply_calendar_charges_for_active_standard(db)
        db.commit()
    except Exception:
        db.rollback()
    week_offset = int(request.args.get('week', 0))
    today = datetime.now()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=week_offset)
    days = [(start_of_week + timedelta(days=i)) for i in range(7)]
    
    day_strings = [d.strftime('%Y-%m-%d') for d in days]
    placeholders = ','.join('?' * 7)
    trainer_filter_sql = ''
    params = list(day_strings)
    if current_role() == 'trainer':
        trainer_filter_sql = trainer_group_filter_sql()
        params.append(current_trainer_id())

    sessions = db.execute(f'''
        SELECT s.*, g.name as group_name,
               {group_trainers_names_subquery('g')} as trainer_name
        FROM schedule s
        JOIN groups g ON s.group_id = g.id
        WHERE s.date IN ({placeholders}) {trainer_filter_sql} ORDER BY s.date, s.time
    ''', params).fetchall()
    intensive_params = list(day_strings)
    intensive_filter_sql = ''
    if current_role() == 'trainer':
        intensive_filter_sql = ' AND i.trainer_id = ?'
        intensive_params.append(current_trainer_id())
    intensives = db.execute(f'''
        SELECT i.*, g.name as group_name, t.name as trainer_name, c.name as client_name
        FROM intensives i
        LEFT JOIN groups g ON g.id = i.group_id
        LEFT JOIN trainers t ON t.id = i.trainer_id
        LEFT JOIN clients c ON c.id = i.client_id
        WHERE i.date IN ({placeholders}) {intensive_filter_sql}
        ORDER BY i.date, i.time
    ''', intensive_params).fetchall()
    
    schedule_by_day = {d: [] for d in day_strings}
    for s in sessions:
        schedule_by_day[s['date']].append(s)
    intensives_by_day = {d: [] for d in day_strings}
    for i in intensives:
        participants = db.execute('''
            SELECT c.id, c.name, ic.payment_type, ic.payment_method, ic.hours, ic.amount, ic.lessons_written_off
            FROM intensive_clients ic
            JOIN clients c ON c.id = ic.client_id
            WHERE ic.intensive_id=?
            ORDER BY CASE WHEN instr(c.name, ' ') > 0 THEN substr(c.name, 1, instr(c.name, ' ')-1) ELSE c.name END COLLATE NOCASE, c.name COLLATE NOCASE
        ''', (i['id'],)).fetchall()
        intensives_by_day[i['date']].append({
            'id': i['id'],
            'time': i['time'],
            'hours': i['hours'],
            'group_name': i['group_name'],
            'trainer_name': i['trainer_name'],
            'client_name': i['client_name'],
            'payment_type': i['payment_type'],
            'payment_method': i['payment_method'],
            'amount': i['amount'],
            'lessons_written_off': i['lessons_written_off'],
            'participants': ', '.join([p['name'] for p in participants]) if participants else '',
            'participants_count': len(participants)
        })
    
    if current_role() == 'trainer':
        groups_list = db.execute('''
            SELECT DISTINCT g.*
            FROM groups g
            JOIN group_trainers gt ON gt.group_id=g.id
            WHERE gt.trainer_id=?
            ORDER BY g.name COLLATE NOCASE
        ''', (current_trainer_id(),)).fetchall()
    else:
        groups_list = db.execute('SELECT * FROM groups ORDER BY name COLLATE NOCASE').fetchall()
    clients_list = db.execute("SELECT * FROM clients ORDER BY CASE WHEN instr(name, ' ') > 0 THEN substr(name, 1, instr(name, ' ')-1) ELSE name END COLLATE NOCASE, name COLLATE NOCASE").fetchall()
    if current_role() == 'trainer':
        trainers_list = db.execute('SELECT * FROM trainers WHERE id=?', (current_trainer_id(),)).fetchall()
    else:
        trainers_list = db.execute('SELECT * FROM trainers ORDER BY name COLLATE NOCASE').fetchall()
    group_members = db.execute('''
        SELECT cg.group_id, c.id as client_id, c.name as client_name
        FROM client_groups cg
        JOIN clients c ON c.id = cg.client_id
        ORDER BY cg.group_id, CASE WHEN instr(c.name, ' ') > 0 THEN substr(c.name, 1, instr(c.name, ' ')-1) ELSE c.name END COLLATE NOCASE, c.name COLLATE NOCASE
    ''').fetchall()
    group_members_by_group = {}
    for row in group_members:
        gid = str(row['group_id'])
        group_members_by_group.setdefault(gid, []).append({
            'id': row['client_id'],
            'name': row['client_name']
        })
    active_subscriptions = db.execute('''
        SELECT s.id, s.client_id, s.lessons_left, s.end_date, c.name as client_name, st.name as type_name
        FROM subscriptions s
        JOIN clients c ON c.id = s.client_id
        JOIN subscription_types st ON st.id = s.type_id
        WHERE s.status='active' AND s.lessons_left > 0
        ORDER BY CASE WHEN instr(c.name, ' ') > 0 THEN substr(c.name, 1, instr(c.name, ' ')-1) ELSE c.name END COLLATE NOCASE, s.end_date
    ''').fetchall()
    active_subscriptions_json = [dict(row) for row in active_subscriptions]
    return render_template('schedule.html', days=days, day_strings=day_strings,
                           schedule_by_day=schedule_by_day, groups=groups_list,
                           intensives_by_day=intensives_by_day,
                           clients=clients_list, active_subscriptions=active_subscriptions_json,
                           group_members_by_group=group_members_by_group,
                           trainers=trainers_list,
                           week_offset=week_offset)

@app.route('/schedule/add', methods=['POST'])
@login_required
def add_schedule():
    db = get_db()
    week = request.form.get('week_offset', 0)
    group_id = request.form.get('group_id')
    mode = request.form.get('schedule_mode', 'single')
    if not group_id:
        flash('Выберите группу', 'error')
        return redirect(url_for('schedule', week=week))
    if current_role() == 'trainer':
        allowed = trainer_in_group(db, group_id, current_trainer_id())
        if not allowed:
            flash('Вы можете добавлять тренировки только своим группам', 'error')
            return redirect(url_for('schedule', week=week))
    duration = int(request.form.get('duration', 60))
    room = request.form.get('room', '')
    notes = request.form.get('notes', '')
    if mode == 'recurring':
        start_date_str = request.form.get('start_date')
        end_date_str = request.form.get('end_date')
        time_value = request.form.get('time')
        weekdays = request.form.getlist('weekdays')
        if not (start_date_str and end_date_str and time_value and weekdays):
            flash('Для авторасписания заполните диапазон дат, время и дни недели', 'error')
            return redirect(url_for('schedule', week=week))
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except ValueError:
            flash('Неверный формат даты', 'error')
            return redirect(url_for('schedule', week=week))
        if end_date < start_date:
            flash('Дата окончания не может быть раньше даты начала', 'error')
            return redirect(url_for('schedule', week=week))
        weekday_numbers = {int(w) for w in weekdays}
        created_count = 0
        skipped_count = 0
        current = start_date
        while current <= end_date:
            if current.weekday() in weekday_numbers:
                date_str = current.strftime('%Y-%m-%d')
                existing = db.execute(
                    'SELECT id FROM schedule WHERE group_id=? AND date=? AND time=?',
                    (group_id, date_str, time_value)
                ).fetchone()
                if existing:
                    skipped_count += 1
                else:
                    db.execute(
                        'INSERT INTO schedule (group_id, date, time, duration_minutes, room, notes) VALUES (?,?,?,?,?,?)',
                        (group_id, date_str, time_value, duration, room, notes)
                    )
                    created_count += 1
            current += timedelta(days=1)
        db.commit()
        if created_count == 0:
            flash(f'Новых занятий не добавлено (пропущено дубликатов: {skipped_count})', 'warning')
        else:
            flash(f'Добавлено занятий: {created_count}. Пропущено дубликатов: {skipped_count}', 'success')
        return redirect(url_for('schedule', week=week))
    date_value = request.form.get('date')
    time_value = request.form.get('time')
    if not (date_value and time_value):
        flash('Для разового занятия укажите дату и время', 'error')
        return redirect(url_for('schedule', week=week))
    db.execute('INSERT INTO schedule (group_id, date, time, duration_minutes, room, notes) VALUES (?,?,?,?,?,?)', (
        group_id, date_value, time_value, duration, room, notes
    ))
    db.commit()
    flash('Занятие добавлено', 'success')
    return redirect(url_for('schedule', week=week))

@app.route('/schedule/add-recurring', methods=['POST'])
@login_required
def add_recurring_schedule():
    db = get_db()
    week = request.form.get('week_offset', 0)
    group_id = request.form.get('group_id')
    if current_role() == 'trainer':
        allowed = trainer_in_group(db, group_id, current_trainer_id())
        if not allowed:
            flash('Вы можете добавлять тренировки только своим группам', 'error')
            return redirect(url_for('schedule', week=week))
    start_date_str = request.form.get('start_date')
    end_date_str = request.form.get('end_date')
    time_value = request.form.get('time')
    weekdays = request.form.getlist('weekdays')

    if not (group_id and start_date_str and end_date_str and time_value and weekdays):
        flash('Заполните группу, даты, время и хотя бы один день недели', 'error')
        return redirect(url_for('schedule', week=week))

    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    except ValueError:
        flash('Неверный формат даты', 'error')
        return redirect(url_for('schedule', week=week))

    if end_date < start_date:
        flash('Дата окончания не может быть раньше даты начала', 'error')
        return redirect(url_for('schedule', week=week))

    weekday_numbers = {int(w) for w in weekdays}
    duration = int(request.form.get('duration', 60))
    room = request.form.get('room', '')
    notes = request.form.get('notes', '')

    created_count = 0
    skipped_count = 0
    current = start_date

    while current <= end_date:
        if current.weekday() in weekday_numbers:
            date_str = current.strftime('%Y-%m-%d')
            existing = db.execute(
                'SELECT id FROM schedule WHERE group_id=? AND date=? AND time=?',
                (group_id, date_str, time_value)
            ).fetchone()

            if existing:
                skipped_count += 1
            else:
                db.execute(
                    'INSERT INTO schedule (group_id, date, time, duration_minutes, room, notes) VALUES (?,?,?,?,?,?)',
                    (group_id, date_str, time_value, duration, room, notes)
                )
                created_count += 1
        current += timedelta(days=1)

    db.commit()

    if created_count == 0:
        flash(f'Новых занятий не добавлено (пропущено дубликатов: {skipped_count})', 'warning')
    else:
        flash(f'Добавлено занятий: {created_count}. Пропущено дубликатов: {skipped_count}', 'success')

    return redirect(url_for('schedule', week=week))

def _save_intensive_participants(db, intensive_id, participant_ids, form, lesson_date=None):
    total_hours = 0.0
    total_amount = 0.0

    hours_raw_common = form.get('hours')
    try:
        common_hours_value = float(hours_raw_common) if hours_raw_common is not None else None
    except ValueError:
        common_hours_value = None
    for cid in participant_ids:
        payment_type = form.get(f'payment_type_{cid}', 'cash')
        payment_method = form.get(f'payment_method_{cid}', 'cash')
        hours_raw = hours_raw_common if hours_raw_common is not None else form.get(f'hours_{cid}', '1')
        subscription_id = form.get(f'subscription_id_{cid}') or None
        lessons_raw = form.get(f'lessons_to_write_off_{cid}', '1')

        try:
            hours = float(hours_raw)
        except ValueError:
            hours = 0
        if hours <= 0:
            raise ValueError('Количество часов должно быть больше нуля для каждого участника')

        try:
            lessons_to_write_off = int(lessons_raw)
        except ValueError:
            lessons_to_write_off = 0

        amount = 0.0
        lessons_written_off = 0

        if payment_type == 'cash':
            if payment_method not in ('cash', 'card'):
                raise ValueError('Для денежной оплаты выберите нал или безнал')
            amount = round(600 * hours, 2)
            cash_amount = amount if payment_method == 'cash' else 0.0
            non_cash_amount = amount if payment_method == 'card' else 0.0
        elif payment_type == 'subscription':
            if not subscription_id:
                raise ValueError('Выберите абонемент для списания')
            if lessons_to_write_off <= 0:
                raise ValueError('Количество списываемых занятий должно быть больше нуля')
            if lesson_date:
                sub = db.execute('''
                    SELECT * FROM subscriptions
                    WHERE id=? AND client_id=? AND status='active' AND lessons_left > 0
                    AND end_date >= ?
                ''', (subscription_id, cid, lesson_date)).fetchone()
            else:
                sub = db.execute('''
                    SELECT * FROM subscriptions
                    WHERE id=? AND client_id=? AND status='active' AND lessons_left > 0
                ''', (subscription_id, cid)).fetchone()
            if not sub:
                raise ValueError('Абонемент для списания недоступен или срок его действия истек')
            if sub['lessons_left'] < lessons_to_write_off:
                raise ValueError('Недостаточно занятий на абонементе')
            new_left = sub['lessons_left'] - lessons_to_write_off
            new_status = 'used' if new_left <= 0 else 'active'
            db.execute('UPDATE subscriptions SET lessons_left=?, status=? WHERE id=?', (new_left, new_status, subscription_id))
            lessons_written_off = lessons_to_write_off
            payment_method = 'cash'
            cash_amount = 0.0
            non_cash_amount = 0.0
        else:
            raise ValueError('Неизвестный тип оплаты участника')

        db.execute('''
            INSERT INTO intensive_clients
            (intensive_id, client_id, payment_type, payment_method, hours, amount, cash_amount, non_cash_amount, subscription_id, lessons_written_off)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        ''', (intensive_id, cid, payment_type, payment_method, hours, amount, cash_amount, non_cash_amount, subscription_id, lessons_written_off))

        # Часы интенсива считаем по интенсиву, а не суммой по людям.
        # total_hours оставляем для обратной совместимости, но будем использовать common_hours_value.
        total_hours += hours
        total_amount += amount

    intensive_hours = common_hours_value if common_hours_value is not None else total_hours
    return intensive_hours, total_amount

@app.route('/intensives/add', methods=['POST'])
@login_required
def add_intensive():
    db = get_db()
    week = request.form.get('week_offset', 0)
    participant_mode = request.form.get('participant_mode', 'clients')
    group_id = request.form.get('group_id') or None
    trainer_id = request.form.get('trainer_id') or None
    date_value = request.form.get('date')
    time_value = request.form.get('time')
    notes = request.form.get('notes', '')

    if not (date_value and time_value and trainer_id):
        flash('Заполните дату, время и тренера', 'error')
        return redirect(url_for('schedule', week=week))
    if current_role() == 'trainer':
        trainer_id = str(current_trainer_id())
        if group_id:
            allowed = trainer_in_group(db, group_id, current_trainer_id())
            if not allowed:
                flash('Вы можете добавлять интенсив только своим группам', 'error')
                return redirect(url_for('schedule', week=week))

    if participant_mode == 'group':
        if not group_id:
            flash('Выберите группу для интенсива', 'error')
            return redirect(url_for('schedule', week=week))
        group_members = db.execute('SELECT client_id FROM client_groups WHERE group_id=?', (group_id,)).fetchall()
        participant_ids = [str(r['client_id']) for r in group_members]
    else:
        participant_ids = list(set(request.form.getlist('client_ids')))

    if not participant_ids:
        flash('Выберите хотя бы одного участника', 'error')
        return redirect(url_for('schedule', week=week))

    try:
        cursor = db.execute('''
            INSERT INTO intensives (group_id, trainer_id, date, time, hours, payment_type, payment_method, amount, notes)
            VALUES (?,?,?,?,?,?,?,?,?)
        ''', (group_id, trainer_id, date_value, time_value, 0, 'cash', 'cash', 0, notes))
        intensive_id = cursor.lastrowid
        total_hours, total_amount = _save_intensive_participants(db, intensive_id, participant_ids, request.form, date_value)
        db.execute('UPDATE intensives SET hours=?, amount=? WHERE id=?', (round(total_hours, 2), round(total_amount, 2), intensive_id))
        db.commit()
    except ValueError as exc:
        db.rollback()
        flash(str(exc), 'error')
        return redirect(url_for('schedule', week=week))

    flash('Интенсив добавлен', 'success')
    return redirect(url_for('schedule', week=week))

@app.route('/individual-lessons/add', methods=['POST'])
@login_required
def add_individual_lesson():
    db = get_db()
    week = request.form.get('week_offset', 0)
    trainer_id = request.form.get('trainer_id')
    client_id = request.form.get('client_id')
    date_value = request.form.get('date')
    time_value = request.form.get('time')
    notes = request.form.get('notes', '')
    payment_option = request.form.get('payment_option', 'cash')
    total_amount = request.form.get('amount', type=float)
    raw_cash_amount = request.form.get('cash_amount', type=float) or 0.0
    raw_non_cash_amount = request.form.get('non_cash_amount', type=float) or 0.0

    if not (date_value and time_value and trainer_id and client_id):
        flash('Заполните дату, время, тренера и клиента', 'error')
        return redirect(url_for('schedule', week=week))
    if total_amount is None or total_amount < 0:
        flash('Укажите корректную сумму оплаты', 'error')
        return redirect(url_for('schedule', week=week))

    if current_role() == 'trainer':
        trainer_id = str(current_trainer_id())

    if payment_option not in ('cash', 'card', 'mixed'):
        flash('Неверный способ оплаты', 'error')
        return redirect(url_for('schedule', week=week))

    if payment_option == 'cash':
        cash_amount = round(total_amount, 2)
        non_cash_amount = 0.0
    elif payment_option == 'card':
        cash_amount = 0.0
        non_cash_amount = round(total_amount, 2)
    else:
        cash_amount = round(raw_cash_amount, 2)
        non_cash_amount = round(raw_non_cash_amount, 2)
        if cash_amount < 0 or non_cash_amount < 0:
            flash('Суммы оплаты не могут быть отрицательными', 'error')
            return redirect(url_for('schedule', week=week))
        if abs((cash_amount + non_cash_amount) - total_amount) > 0.01:
            flash('При разделении оплаты сумма наличных и безналичных должна равняться общей сумме', 'error')
            return redirect(url_for('schedule', week=week))

    try:
        cursor = db.execute('''
            INSERT INTO intensives (group_id, trainer_id, date, time, hours, payment_type, payment_method, amount, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (None, trainer_id, date_value, time_value, 1, 'cash', payment_option, round(total_amount, 2), notes))
        intensive_id = cursor.lastrowid
        db.execute('''
            INSERT INTO intensive_clients
            (intensive_id, client_id, payment_type, payment_method, hours, amount, cash_amount, non_cash_amount, subscription_id, lessons_written_off)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (intensive_id, client_id, 'cash', payment_option, 1, round(total_amount, 2), cash_amount, non_cash_amount, None, 0))
        db.commit()
    except Exception:
        db.rollback()
        flash('Не удалось создать индивидуальное занятие', 'error')
        return redirect(url_for('schedule', week=week))

    flash('Индивидуальное занятие добавлено', 'success')
    return redirect(url_for('schedule', week=week))

@app.route('/intensives/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_intensive(id):
    db = get_db()
    intensive = db.execute('SELECT * FROM intensives WHERE id=?', (id,)).fetchone()
    if current_role() == 'trainer' and intensive['trainer_id'] != current_trainer_id():
        flash('Вы можете редактировать только свои интенсивы', 'error')
        return redirect(url_for('schedule'))
    if not intensive:
        flash('Интенсив не найден', 'error')
        return redirect(url_for('schedule'))

    if request.method == 'POST':
        trainer_id = request.form.get('trainer_id') or None
        date_value = request.form.get('date')
        time_value = request.form.get('time')
        notes = request.form.get('notes', '')
        participant_ids = request.form.getlist('client_ids')
        if not participant_ids:
            flash('Выберите хотя бы одного участника', 'error')
            return redirect(url_for('edit_intensive', id=id))
        try:
            previous_sub_writes = db.execute('''
                SELECT subscription_id, lessons_written_off
                FROM intensive_clients
                WHERE intensive_id=? AND payment_type='subscription' AND subscription_id IS NOT NULL
            ''', (id,)).fetchall()
            for row in previous_sub_writes:
                db.execute('''
                    UPDATE subscriptions
                    SET lessons_left = lessons_left + ?, status='active'
                    WHERE id=?
                ''', (row['lessons_written_off'] or 0, row['subscription_id']))

            db.execute('''
                UPDATE intensives SET trainer_id=?, date=?, time=?, notes=?
                WHERE id=?
            ''', (trainer_id, date_value, time_value, notes, id))
            db.execute('DELETE FROM intensive_clients WHERE intensive_id=?', (id,))
            total_hours, total_amount = _save_intensive_participants(db, id, participant_ids, request.form, date_value)
            db.execute('UPDATE intensives SET hours=?, amount=? WHERE id=?', (round(total_hours, 2), round(total_amount, 2), id))
            db.commit()
            flash('Интенсив обновлен', 'success')
            return redirect(url_for('schedule'))
        except ValueError as exc:
            db.rollback()
            flash(str(exc), 'error')

    participants = db.execute('''
        SELECT ic.*, c.name as client_name
        FROM intensive_clients ic
        JOIN clients c ON c.id = ic.client_id
        WHERE ic.intensive_id=?
        ORDER BY CASE WHEN instr(c.name, ' ') > 0 THEN substr(c.name, 1, instr(c.name, ' ')-1) ELSE c.name END COLLATE NOCASE, c.name COLLATE NOCASE
    ''', (id,)).fetchall()

    # For group intensives include newly added group members in edit form
    # so admin can immediately set payment settings for them.
    if intensive['group_id']:
        existing_ids = {row['client_id'] for row in participants}
        group_members = db.execute('''
            SELECT c.id as client_id, c.name as client_name
            FROM client_groups cg
            JOIN clients c ON c.id = cg.client_id
            WHERE cg.group_id=?
            ORDER BY CASE WHEN instr(c.name, ' ') > 0 THEN substr(c.name, 1, instr(c.name, ' ')-1) ELSE c.name END COLLATE NOCASE, c.name COLLATE NOCASE
        ''', (intensive['group_id'],)).fetchall()

        participants_list = [dict(row) for row in participants]
        for gm in group_members:
            if gm['client_id'] not in existing_ids:
                participants_list.append({
                    'intensive_id': id,
                    'client_id': gm['client_id'],
                    'client_name': gm['client_name'],
                    'payment_type': 'cash',
                    'payment_method': 'cash',
                    'hours': 1,
                    'amount': 600,
                    'subscription_id': None,
                    'lessons_written_off': 0
                })
        participants_list.sort(key=lambda x: x['client_name'])
        participants = participants_list

    clients_list = db.execute("SELECT * FROM clients ORDER BY CASE WHEN instr(name, ' ') > 0 THEN substr(name, 1, instr(name, ' ')-1) ELSE name END COLLATE NOCASE, name COLLATE NOCASE").fetchall()
    trainers_list = db.execute('SELECT * FROM trainers ORDER BY name').fetchall()
    active_subscriptions = db.execute('''
        SELECT s.id, s.client_id, s.lessons_left, s.end_date, st.name as type_name
        FROM subscriptions s
        JOIN subscription_types st ON st.id = s.type_id
        WHERE s.status='active' AND s.lessons_left > 0
        ORDER BY s.client_id, s.end_date
    ''').fetchall()
    return render_template(
        'intensive_edit.html',
        intensive=intensive,
        participants=participants,
        clients=clients_list,
        trainers=trainers_list,
        active_subscriptions=active_subscriptions
    )

@app.route('/intensives/<int:id>/delete', methods=['POST'])
@login_required
def delete_intensive(id):
    db = get_db()
    week = request.form.get('week_offset', 0)

    intensive = db.execute('SELECT * FROM intensives WHERE id=?', (id,)).fetchone()
    if not intensive:
        flash('Интенсив не найден', 'warning')
        return redirect(url_for('schedule', week=week))
    if current_role() == 'trainer' and intensive['trainer_id'] != current_trainer_id():
        flash('Вы можете удалять только свои интенсивы', 'error')
        return redirect(url_for('schedule', week=week))

    db.execute('DELETE FROM intensives WHERE id=?', (id,))
    db.commit()
    flash('Интенсив удалён', 'success')
    return redirect(url_for('schedule', week=week))

@app.route('/schedule/delete/<int:id>', methods=['POST'])
@login_required
def delete_schedule(id):
    db = get_db()
    week = request.form.get('week_offset', 0)
    db.execute('DELETE FROM schedule WHERE id=?', (id,))
    db.commit()
    flash('Занятие удалено', 'success')
    return redirect(url_for('schedule', week=week))

@app.route('/schedule/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_schedule(id):
    db = get_db()
    lesson = db.execute('SELECT * FROM schedule WHERE id=?', (id,)).fetchone()
    if not lesson:
        flash('Занятие не найдено', 'error')
        return redirect(url_for('schedule'))

    group = db.execute('SELECT * FROM groups WHERE id=?', (lesson['group_id'],)).fetchone()
    if current_role() == 'trainer':
        allowed = db.execute('SELECT id FROM groups WHERE id=? AND trainer_id=?', (lesson['group_id'], current_trainer_id())).fetchone()
        if not allowed:
            flash('Вы можете редактировать только занятия своих групп', 'error')
            return redirect(url_for('schedule'))

    if request.method == 'POST':
        mode = request.form.get('edit_mode', 'single')
        week = request.form.get('week_offset', 0)
        time_value = request.form.get('time')
        duration = int(request.form.get('duration', lesson['duration_minutes'] or 60))
        room = request.form.get('room', lesson['room'] or '')
        notes = request.form.get('notes', lesson['notes'] or '')

        if not time_value:
            flash('Укажите время', 'error')
            return redirect(url_for('edit_schedule', id=id))

        if mode == 'recurring':
            start_date_str = request.form.get('start_date')
            end_date_str = request.form.get('end_date')
            weekdays = request.form.getlist('weekdays')
            if not (start_date_str and end_date_str and weekdays):
                flash('Для авторасписания заполните диапазон дат и дни недели', 'error')
                return redirect(url_for('edit_schedule', id=id))
            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            except ValueError:
                flash('Неверный формат даты', 'error')
                return redirect(url_for('edit_schedule', id=id))
            if end_date < start_date:
                flash('Дата окончания не может быть раньше даты начала', 'error')
                return redirect(url_for('edit_schedule', id=id))

            weekday_numbers = {int(w) for w in weekdays}
            current = start_date
            updated = 0
            while current <= end_date:
                if current.weekday() in weekday_numbers:
                    date_str = current.strftime('%Y-%m-%d')
                    db.execute('''
                        UPDATE schedule
                        SET time=?, duration_minutes=?, room=?, notes=?
                        WHERE group_id=? AND date=?
                    ''', (time_value, duration, room, notes, lesson['group_id'], date_str))
                    updated += db.total_changes
                current += timedelta(days=1)

            db.commit()
            flash('Авторасписание обновлено', 'success')
            return redirect(url_for('schedule', week=week))

        # single
        db.execute('''
            UPDATE schedule SET time=?, duration_minutes=?, room=?, notes=? WHERE id=?
        ''', (time_value, duration, room, notes, id))
        db.commit()
        flash('Занятие обновлено', 'success')
        return redirect(url_for('schedule', week=week))

    return render_template('schedule_edit.html', lesson=lesson, group=group)

@app.route('/schedule/mark-attendance/<int:schedule_id>', methods=['POST'])
@login_required
def mark_attendance(schedule_id):
    db = get_db()
    session_data = db.execute('SELECT * FROM schedule WHERE id=?', (schedule_id,)).fetchone()
    if current_role() == 'trainer':
        allowed = db.execute('''
            SELECT s.id FROM schedule s
            JOIN groups g ON g.id=s.group_id
            WHERE s.id=?
            AND EXISTS (SELECT 1 FROM group_trainers gt WHERE gt.group_id=g.id AND gt.trainer_id=?)
        ''', (schedule_id, current_trainer_id())).fetchone()
        if not allowed:
            flash('Вы можете отмечать посещаемость только своих тренировок', 'error')
            return redirect(url_for('schedule'))
    new_present = {int(cid) for cid in request.form.getlist('present')}
    members = db.execute('''
        SELECT c.id
        FROM clients c
        JOIN client_groups cg ON c.id=cg.client_id
        WHERE cg.group_id=?
    ''', (session_data['group_id'],)).fetchall()

    for m in members:
        cid = m['id']
        is_present = 1 if cid in new_present else 0
        attendance_row = db.execute('''
            SELECT * FROM attendance
            WHERE schedule_id=? AND client_id=?
        ''', (schedule_id, cid)).fetchone()

        if attendance_row:
            db.execute('UPDATE attendance SET present=? WHERE id=?', (is_present, attendance_row['id']))
        else:
            db.execute('''
                INSERT INTO attendance (schedule_id, client_id, present, charged_lessons, charged_subscription_id)
                VALUES (?,?,?,?,?)
            ''', (schedule_id, cid, is_present, 0, None))
            attendance_row = db.execute('''
                SELECT * FROM attendance WHERE schedule_id=? AND client_id=?
            ''', (schedule_id, cid)).fetchone()

        # Стандарт (carry_over=0) списывается всегда по календарю.
        # Стандарт+ (carry_over=1) списывается только при отметке присутствия.
        standard_sub = db.execute('''
            SELECT s.*
            FROM subscriptions s
            JOIN subscription_types st ON st.id = s.type_id
            WHERE s.client_id=?
            AND (s.group_id=? OR s.group_id IS NULL)
            AND s.status='active' AND s.lessons_left > 0
            AND s.end_date >= ?
            AND st.carry_over=0
            ORDER BY s.end_date LIMIT 1
        ''', (cid, session_data['group_id'], session_data['date'])).fetchone()
        standard_plus_sub = db.execute('''
            SELECT s.*
            FROM subscriptions s
            JOIN subscription_types st ON st.id = s.type_id
            WHERE s.client_id=?
            AND (s.group_id=? OR s.group_id IS NULL)
            AND s.status='active' AND s.lessons_left > 0
            AND s.end_date >= ?
            AND st.carry_over=1
            ORDER BY s.end_date LIMIT 1
        ''', (cid, session_data['group_id'], session_data['date'])).fetchone()
        # ВАЖНО:
        # - Стандарт (carry_over=0) списывается по календарю (в schedule()), не в отметке.
        # - Стандарт+ (carry_over=1) списывается только при отметке присутствия.
        desired_sub = standard_plus_sub if is_present else None
        desired_sub_id = desired_sub['id'] if desired_sub else None
        already_charged = attendance_row['charged_lessons'] == 1
        charged_sub_id = attendance_row['charged_subscription_id']

        if already_charged and charged_sub_id and desired_sub_id != charged_sub_id:
            if charged_sub_id:
                sub = db.execute('SELECT * FROM subscriptions WHERE id=?', (charged_sub_id,)).fetchone()
                if sub:
                    db.execute(
                        'UPDATE subscriptions SET lessons_left=?, status=? WHERE id=?',
                        (sub['lessons_left'] + 1, 'active', charged_sub_id)
                    )
            db.execute(
                'UPDATE attendance SET charged_lessons=0, charged_subscription_id=NULL WHERE id=?',
                (attendance_row['id'],)
            )
            already_charged = False

        if desired_sub and not already_charged:
            new_left = desired_sub['lessons_left'] - 1
            new_status = 'used' if new_left <= 0 else 'active'
            db.execute(
                'UPDATE subscriptions SET lessons_left=?, status=? WHERE id=?',
                (new_left, new_status, desired_sub_id)
            )
            db.execute(
                'UPDATE attendance SET charged_lessons=1, charged_subscription_id=? WHERE id=?',
                (desired_sub_id, attendance_row['id'])
            )
            already_charged = True

        if (not desired_sub) and already_charged:
            charged_sub_id = attendance_row['charged_subscription_id']
            if charged_sub_id:
                sub = db.execute('SELECT * FROM subscriptions WHERE id=?', (charged_sub_id,)).fetchone()
                if sub:
                    db.execute(
                        'UPDATE subscriptions SET lessons_left=?, status=? WHERE id=?',
                        (sub['lessons_left'] + 1, 'active', charged_sub_id)
                    )
            db.execute(
                'UPDATE attendance SET charged_lessons=0, charged_subscription_id=NULL WHERE id=?',
                (attendance_row['id'],)
            )

    db.commit()
    flash('Посещаемость отмечена', 'success')
    return redirect(url_for('schedule'))

@app.route('/schedule/<int:id>/attendance')
@login_required
def attendance_form(id):
    db = get_db()
    session_data = db.execute('''SELECT s.*, g.name as group_name FROM schedule s
        JOIN groups g ON s.group_id=g.id WHERE s.id=?''', (id,)).fetchone()
    if current_role() == 'trainer':
        group_owner = trainer_in_group(db, session_data['group_id'], current_trainer_id())
        if not group_owner:
            flash('Вы можете открывать посещаемость только своих тренировок', 'error')
            return redirect(url_for('schedule'))
    members = db.execute('''SELECT c.* FROM clients c
        JOIN client_groups cg ON c.id=cg.client_id WHERE cg.group_id=?''',
        (session_data['group_id'],)).fetchall()
    attended = [r['client_id'] for r in db.execute(
        'SELECT client_id FROM attendance WHERE schedule_id=? AND present=1', (id,)).fetchall()]

    subscription_info_by_client = {}
    for member in members:
        sub = db.execute('''SELECT s.lessons_left, s.end_date, st.validity_days, st.name as type_name, st.carry_over
            FROM subscriptions s
            JOIN subscription_types st ON st.id=s.type_id
            WHERE client_id=?
            AND (group_id=? OR group_id IS NULL)
            AND status='active' AND lessons_left > 0
            AND end_date >= ?
            ORDER BY end_date LIMIT 1''',
            (member['id'], session_data['group_id'], session_data['date'])).fetchone()
        if sub:
            subscription_info_by_client[member['id']] = {
                'lessons_left': sub['lessons_left'],
                'end_date': sub['end_date'],
                'validity_days': sub['validity_days'],
                'type_name': sub['type_name'],
                'carry_over': sub['carry_over']
            }
        else:
            subscription_info_by_client[member['id']] = None

    return render_template(
        'attendance.html',
        lesson=session_data,
        members=members,
        attended=attended,
        subscription_info_by_client=subscription_info_by_client
    )

@app.route('/sell_subscription/<int:client_id>', methods=['GET', 'POST'])
@login_required
def sell_subscription(client_id):
    db = get_db()
    client = db.execute('SELECT * FROM clients WHERE id = ?', (client_id,)).fetchone()
    if not client:
        flash('Клиент не найден.', 'error')
        return redirect(url_for('schedule'))

    if request.method == 'POST':
        subscription_type = request.form.get('subscription_type', type=int)
        payment_option = request.form.get('payment_option', 'cash')
        discount_percent = request.form.get('discount_percent', type=float) or 0.0
        manual_cash_amount = request.form.get('cash_amount', type=float) or 0.0
        manual_non_cash_amount = request.form.get('non_cash_amount', type=float) or 0.0

        if not subscription_type:
            flash('Выберите тип абонемента.', 'error')
            return redirect(request.url)

        stype = db.execute('SELECT * FROM subscription_types WHERE id=?', (subscription_type,)).fetchone()
        if not stype:
            flash('Тип абонемента не найден.', 'error')
            return redirect(request.url)

        base_price = float(stype['price'])
        if discount_percent < 0:
            flash('Скидка не может быть отрицательной.', 'error')
            return redirect(request.url)
        if discount_percent > 100:
            flash('Скидка не может быть больше 100%.', 'error')
            return redirect(request.url)

        discount_amount = round(base_price * (discount_percent / 100.0), 2)
        price_paid = round(base_price - discount_amount, 2)
        if price_paid < 0:
            price_paid = 0.0

        if payment_option not in ('cash', 'card', 'mixed'):
            flash('Неверный способ оплаты.', 'error')
            return redirect(request.url)

        if payment_option == 'cash':
            payment_method = 'cash'
            cash_amount = price_paid
            non_cash_amount = 0.0
        elif payment_option == 'card':
            payment_method = 'card'
            cash_amount = 0.0
            non_cash_amount = price_paid
        else:
            payment_method = 'mixed'
            cash_amount = round(manual_cash_amount, 2)
            non_cash_amount = round(manual_non_cash_amount, 2)
            if cash_amount < 0 or non_cash_amount < 0:
                flash('Суммы оплаты не могут быть отрицательными.', 'error')
                return redirect(request.url)
            if abs((cash_amount + non_cash_amount) - price_paid) > 0.01:
                flash('При смешанной оплате сумма нал + безнал должна равняться итоговой цене.', 'error')
                return redirect(request.url)

        activation_date = datetime.now().strftime('%Y-%m-%d')
        end_date = (datetime.strptime(activation_date, '%Y-%m-%d') + timedelta(days=stype['validity_days'])).strftime('%Y-%m-%d')

        db.execute(
            '''INSERT INTO subscriptions
               (client_id, type_id, group_id, lessons_total, lessons_left, start_date, end_date, price_paid, payment_method, cash_amount, non_cash_amount, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                client_id, subscription_type, None,
                stype['lessons_count'], stype['lessons_count'],
                activation_date, end_date,
                price_paid, payment_method, cash_amount, non_cash_amount, 'active'
            )
        )
        db.commit()
        flash('Абонемент успешно продан.', 'success')
        return redirect(url_for('schedule'))

    subscription_types = db.execute('SELECT * FROM subscription_types').fetchall()
    return render_template('sell_subscription.html', client=client, subscription_types=subscription_types)

if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=8050)
