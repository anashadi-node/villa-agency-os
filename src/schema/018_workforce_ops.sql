-- Migration 018: Workforce Ops — staff, roster, attendance, timesheet, capacity-bound task
-- assignment, and the payroll boundary (ADR-0071).
--
-- 001_initial.sql carries the same shapes, so a freshly created database and a migrated one
-- hold the same contract. Every statement here is idempotent — running twice changes nothing.
--
-- What this migration owns. ADR-0071 draws one line through the whole file: the *work*
-- (who is on shift, who was actually present, what the timesheet says, how much of a person's
-- day is already committed) is ordinary operational data, readable by a supervisor; the *pay*
-- (rates, payslips, statutory deductions) is a separate, encrypted, human-approved ledger that
-- a supervisor cannot read and no agent can write.
--
-- Three hard rules live in the storage layer rather than in a service, because a service can
-- be bypassed by a script and a constraint cannot:
--
--   1. a person is never on two overlapping published shifts (UNIQUE start per staff + the
--      writer's overlap check in Sprint 2; PostgreSQL gets the declarative exclusion in 002's
--      dialect work);
--   2. attendance evidence is append-only (triggers below) — a correction is a new
--      MANUAL_ADJUST event that names its evidence, never an edit of history;
--   3. money arithmetic on a payslip is closed by CHECK: net = gross - deduction, with every
--      line non-negative and every amount an exact integer minor unit. No float ever reaches
--      this table.
--
-- What this migration deliberately does NOT store: raw coordinates, national identity numbers,
-- bank account numbers, tax identifiers, or card data. Identity and bank fields exist only as
-- ciphertext in workforce_staff_private (key-id + algorithm + wrapped DEK + AAD context), so a
-- database dump alone cannot re-identify a person or move money. Geofence evidence is stored
-- as a verdict plus an accuracy bucket, never as a position.

-- ---------------------------------------------------------------------------
-- 1. Policy as versioned data, never as a prompt (ADR-0071 §3, mirrors ADR-0032's rule).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workforce_policy_versions (
    policy_version_id TEXT NOT NULL,
    agency_id          TEXT NOT NULL DEFAULT 'agency_default',
    policy_code        TEXT NOT NULL CHECK (policy_code IN (
                           'WORK_HOURS','OVERTIME','BREAK','LEAVE','PAYROLL_TAX',
                           'PAYROLL_BPJS','ATTENDANCE_GEOFENCE','TASK_CAPACITY')),
    version            INTEGER NOT NULL CHECK (version >= 1),
    payload_json       TEXT NOT NULL CHECK (length(payload_json) >= 2),
    checksum_sha256    TEXT NOT NULL CHECK (length(checksum_sha256) = 64),
    effective_from     TEXT NOT NULL,
    effective_to       TEXT,
    approved_by        TEXT NOT NULL CHECK (length(approved_by) >= 3),
    approved_at        TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    CONSTRAINT pk_workforce_policy_versions PRIMARY KEY (policy_version_id),
    CONSTRAINT uq_workforce_policy_versions UNIQUE (agency_id, policy_code, version),
    CONSTRAINT ck_workforce_policy_window CHECK (effective_to IS NULL OR effective_to > effective_from),
    CONSTRAINT fk_workforce_policy_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id)
);
CREATE INDEX IF NOT EXISTS ix_workforce_policy_versions_lookup
    ON workforce_policy_versions (agency_id, policy_code, effective_from);

-- ---------------------------------------------------------------------------
-- 2. Staff master. Display identity only; legal identity lives in ..._private.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workforce_staff (
    staff_id               TEXT NOT NULL,
    agency_id              TEXT NOT NULL DEFAULT 'agency_default',
    employee_no            TEXT NOT NULL CHECK (length(employee_no) BETWEEN 1 AND 32),
    display_name           TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    role_code              TEXT NOT NULL CHECK (role_code IN (
                               'HOUSEKEEPING','FRONT_DESK','MAINTENANCE','SUPERVISOR',
                               'MANAGER','DRIVER','SECURITY','ACCOUNTING')),
    employment_type        TEXT NOT NULL CHECK (employment_type IN (
                               'PERMANENT','CONTRACT','DAILY','INTERN')),
    home_property_id       TEXT,
    weekly_contract_minutes INTEGER NOT NULL DEFAULT 2400
                               CHECK (weekly_contract_minutes BETWEEN 0 AND 2400),
    status                 TEXT NOT NULL DEFAULT 'ACTIVE'
                               CHECK (status IN ('ACTIVE','SUSPENDED','OFFBOARDED')),
    hired_on               TEXT NOT NULL,
    ended_on               TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    version                INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    CONSTRAINT pk_workforce_staff PRIMARY KEY (staff_id),
    CONSTRAINT uq_workforce_staff_employee_no UNIQUE (agency_id, employee_no),
    CONSTRAINT ck_workforce_staff_dates CHECK (ended_on IS NULL OR ended_on >= hired_on),
    -- An offboarded person must carry the date the relationship ended: the payroll ledger
    -- reads that date to decide the final period, so it may never be inferred.
    CONSTRAINT ck_workforce_staff_offboarded CHECK (status <> 'OFFBOARDED' OR ended_on IS NOT NULL),
    CONSTRAINT fk_workforce_staff_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_workforce_staff_property FOREIGN KEY (home_property_id) REFERENCES properties (property_id)
);
CREATE INDEX IF NOT EXISTS ix_workforce_staff_status ON workforce_staff (agency_id, status, role_code);

-- Encrypted companion. One row per staff member; every PII column is ciphertext or NULL.
CREATE TABLE IF NOT EXISTS workforce_staff_private (
    staff_id        TEXT NOT NULL,
    key_id          TEXT NOT NULL CHECK (length(key_id) >= 4),
    alg             TEXT NOT NULL CHECK (alg IN ('AES-256-GCM','XCHACHA20-POLY1305')),
    aad_context     TEXT NOT NULL CHECK (length(aad_context) >= 8),
    wrapped_dek     TEXT NOT NULL CHECK (length(wrapped_dek) >= 32),
    national_id_ct  TEXT CHECK (national_id_ct IS NULL OR length(national_id_ct) >= 32),
    tax_id_ct       TEXT CHECK (tax_id_ct IS NULL OR length(tax_id_ct) >= 32),
    bank_account_ct TEXT CHECK (bank_account_ct IS NULL OR length(bank_account_ct) >= 32),
    contact_ct      TEXT CHECK (contact_ct IS NULL OR length(contact_ct) >= 32),
    updated_at      TEXT NOT NULL,
    rotation_due_at TEXT,
    CONSTRAINT pk_workforce_staff_private PRIMARY KEY (staff_id),
    CONSTRAINT fk_workforce_staff_private_staff FOREIGN KEY (staff_id) REFERENCES workforce_staff (staff_id)
);

-- ---------------------------------------------------------------------------
-- 3. Capability and eligibility: who can do what, where.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workforce_skills (
    skill_id      TEXT NOT NULL,
    agency_id     TEXT NOT NULL DEFAULT 'agency_default',
    staff_id      TEXT NOT NULL,
    skill_code    TEXT NOT NULL CHECK (skill_code IN (
                      'HOUSEKEEPING','LAUNDRY','POOL_CARE','GARDENING','ELECTRICAL',
                      'PLUMBING','AC_SERVICE','GUEST_RELATIONS','DRIVING','NIGHT_AUDIT',
                      'FOOD_SAFETY','FIRST_AID')),
    level         INTEGER NOT NULL CHECK (level BETWEEN 1 AND 5),
    certified_on  TEXT,
    evidence_ref  TEXT,
    created_at    TEXT NOT NULL,
    CONSTRAINT pk_workforce_skills PRIMARY KEY (skill_id),
    CONSTRAINT uq_workforce_skills UNIQUE (staff_id, skill_code),
    CONSTRAINT ck_workforce_skills_evidence CHECK (certified_on IS NULL OR evidence_ref IS NOT NULL),
    CONSTRAINT fk_workforce_skills_staff FOREIGN KEY (staff_id) REFERENCES workforce_staff (staff_id)
);
CREATE INDEX IF NOT EXISTS ix_workforce_skills_code ON workforce_skills (agency_id, skill_code, level);

CREATE TABLE IF NOT EXISTS workforce_property_eligibility (
    agency_id   TEXT NOT NULL DEFAULT 'agency_default',
    staff_id    TEXT NOT NULL,
    property_id TEXT NOT NULL,
    eligible    INTEGER NOT NULL CHECK (eligible IN (0, 1)),
    reason      TEXT,
    decided_by  TEXT NOT NULL CHECK (length(decided_by) >= 3),
    decided_at  TEXT NOT NULL,
    CONSTRAINT pk_workforce_property_eligibility PRIMARY KEY (staff_id, property_id),
    -- A refusal is a decision and must say why; an allowance may stand silent.
    CONSTRAINT ck_workforce_eligibility_reason CHECK (
        eligible = 1 OR (reason IS NOT NULL AND length(reason) >= 3)),
    CONSTRAINT fk_workforce_eligibility_staff FOREIGN KEY (staff_id) REFERENCES workforce_staff (staff_id),
    CONSTRAINT fk_workforce_eligibility_property FOREIGN KEY (property_id) REFERENCES properties (property_id)
);
CREATE INDEX IF NOT EXISTS ix_workforce_eligibility_property
    ON workforce_property_eligibility (property_id, eligible);

-- ---------------------------------------------------------------------------
-- 4. Roster: the shift template (a shape) and the rostered instance (a commitment).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workforce_shift_templates (
    shift_template_id     TEXT NOT NULL,
    agency_id             TEXT NOT NULL DEFAULT 'agency_default',
    property_id           TEXT,
    shift_code            TEXT NOT NULL CHECK (shift_code IN (
                              'MORNING','MIDDAY','EVENING','NIGHT','ON_CALL')),
    starts_local          TEXT NOT NULL CHECK (length(starts_local) = 5),
    ends_local            TEXT NOT NULL CHECK (length(ends_local) = 5),
    break_minutes         INTEGER NOT NULL DEFAULT 30 CHECK (break_minutes BETWEEN 0 AND 180),
    crosses_midnight      INTEGER NOT NULL DEFAULT 0 CHECK (crosses_midnight IN (0, 1)),
    required_headcount    INTEGER NOT NULL DEFAULT 1 CHECK (required_headcount >= 1),
    max_overtime_minutes  INTEGER NOT NULL DEFAULT 0 CHECK (max_overtime_minutes BETWEEN 0 AND 240),
    effective_from        TEXT NOT NULL,
    effective_to          TEXT,
    CONSTRAINT pk_workforce_shift_templates PRIMARY KEY (shift_template_id),
    CONSTRAINT uq_workforce_shift_templates UNIQUE (agency_id, property_id, shift_code, effective_from),
    CONSTRAINT ck_workforce_shift_template_window CHECK (effective_to IS NULL OR effective_to > effective_from),
    CONSTRAINT ck_workforce_shift_template_hours CHECK (starts_local <> ends_local),
    CONSTRAINT fk_workforce_shift_templates_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id),
    CONSTRAINT fk_workforce_shift_templates_property FOREIGN KEY (property_id) REFERENCES properties (property_id)
);

CREATE TABLE IF NOT EXISTS workforce_roster_entries (
    roster_entry_id     TEXT NOT NULL,
    agency_id           TEXT NOT NULL DEFAULT 'agency_default',
    staff_id            TEXT NOT NULL,
    shift_template_id   TEXT NOT NULL,
    property_id         TEXT NOT NULL,
    work_date           TEXT NOT NULL CHECK (length(work_date) = 10),
    starts_at_utc       TEXT NOT NULL,
    ends_at_utc         TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN (
                            'DRAFT','PUBLISHED','CONFIRMED','SWAPPED','CANCELLED','NO_SHOW')),
    published_by        TEXT,
    published_at        TEXT,
    replaces_entry_id   TEXT,
    reason              TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    version             INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    CONSTRAINT pk_workforce_roster_entries PRIMARY KEY (roster_entry_id),
    -- One start instant per person per instant: the cheapest possible overlap refusal, and the
    -- one a supervisor cannot talk their way around.
    CONSTRAINT uq_workforce_roster_start UNIQUE (agency_id, staff_id, starts_at_utc),
    CONSTRAINT ck_workforce_roster_order CHECK (ends_at_utc > starts_at_utc),
    -- A published shift is an announcement: it names who published it and when.
    CONSTRAINT ck_workforce_roster_published CHECK (
        status = 'DRAFT' OR (published_by IS NOT NULL AND published_at IS NOT NULL)),
    -- A swap or a cancellation is a decision: it carries a reason.
    CONSTRAINT ck_workforce_roster_reason CHECK (
        status NOT IN ('SWAPPED','CANCELLED','NO_SHOW')
        OR (reason IS NOT NULL AND length(reason) >= 3)),
    CONSTRAINT fk_workforce_roster_staff FOREIGN KEY (staff_id) REFERENCES workforce_staff (staff_id),
    CONSTRAINT fk_workforce_roster_template FOREIGN KEY (shift_template_id)
        REFERENCES workforce_shift_templates (shift_template_id),
    CONSTRAINT fk_workforce_roster_property FOREIGN KEY (property_id) REFERENCES properties (property_id),
    CONSTRAINT fk_workforce_roster_replaces FOREIGN KEY (replaces_entry_id)
        REFERENCES workforce_roster_entries (roster_entry_id)
);
CREATE INDEX IF NOT EXISTS ix_workforce_roster_staff_date ON workforce_roster_entries (agency_id, staff_id, work_date);
CREATE INDEX IF NOT EXISTS ix_workforce_roster_property_date ON workforce_roster_entries (agency_id, property_id, work_date, status);

-- ---------------------------------------------------------------------------
-- 5. Attendance: evidence first (append-only), derivation second (recomputable).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workforce_attendance_events (
    attendance_event_id TEXT NOT NULL,
    agency_id           TEXT NOT NULL DEFAULT 'agency_default',
    staff_id            TEXT NOT NULL,
    roster_entry_id     TEXT,
    event_type          TEXT NOT NULL CHECK (event_type IN (
                            'CLOCK_IN','CLOCK_OUT','BREAK_START','BREAK_END',
                            'MANUAL_ADJUST','DEVICE_PING','GEOFENCE_REJECT')),
    occurred_at_utc     TEXT NOT NULL,
    recorded_at_utc     TEXT NOT NULL,
    source              TEXT NOT NULL CHECK (source IN ('DEVICE','MANUAL','IMPORT','API')),
    device_ref          TEXT,
    -- Minimisation: a verdict and a coarse accuracy, never a coordinate pair.
    geofence_verdict    TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (geofence_verdict IN (
                            'INSIDE','OUTSIDE','UNKNOWN')),
    accuracy_bucket_m   INTEGER CHECK (accuracy_bucket_m IS NULL OR accuracy_bucket_m >= 0),
    evidence_ref        TEXT,
    actor_id            TEXT NOT NULL CHECK (length(actor_id) >= 3),
    idempotency_key     TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    CONSTRAINT pk_workforce_attendance_events PRIMARY KEY (attendance_event_id),
    CONSTRAINT uq_workforce_attendance_idem UNIQUE (agency_id, idempotency_key),
    -- A manual adjustment is a human act, and it must name its evidence.
    CONSTRAINT ck_workforce_attendance_manual CHECK (
        source <> 'MANUAL' OR (evidence_ref IS NOT NULL AND length(evidence_ref) >= 3)),
    CONSTRAINT ck_workforce_attendance_recorded CHECK (recorded_at_utc >= occurred_at_utc),
    CONSTRAINT fk_workforce_attendance_staff FOREIGN KEY (staff_id) REFERENCES workforce_staff (staff_id),
    CONSTRAINT fk_workforce_attendance_roster FOREIGN KEY (roster_entry_id)
        REFERENCES workforce_roster_entries (roster_entry_id)
);
CREATE INDEX IF NOT EXISTS ix_workforce_attendance_staff_time
    ON workforce_attendance_events (agency_id, staff_id, occurred_at_utc);

CREATE TRIGGER IF NOT EXISTS trg_workforce_attendance_no_update
BEFORE UPDATE ON workforce_attendance_events
BEGIN
    SELECT RAISE(ABORT, 'workforce_attendance_events is append-only (UPDATE rejected)');
END;
CREATE TRIGGER IF NOT EXISTS trg_workforce_attendance_no_delete
BEFORE DELETE ON workforce_attendance_events
BEGIN
    SELECT RAISE(ABORT, 'workforce_attendance_events is append-only (DELETE rejected)');
END;

-- The day is a derivation of the events, not a second truth: it carries the event count it was
-- computed from, so a reader can tell a computed day from a stale one.
CREATE TABLE IF NOT EXISTS workforce_attendance_days (
    agency_id          TEXT NOT NULL DEFAULT 'agency_default',
    staff_id           TEXT NOT NULL,
    work_date          TEXT NOT NULL CHECK (length(work_date) = 10),
    first_in_utc       TEXT,
    last_out_utc       TEXT,
    worked_minutes     INTEGER NOT NULL DEFAULT 0 CHECK (worked_minutes >= 0),
    break_minutes      INTEGER NOT NULL DEFAULT 0 CHECK (break_minutes >= 0),
    overtime_minutes   INTEGER NOT NULL DEFAULT 0 CHECK (overtime_minutes BETWEEN 0 AND 240),
    late_minutes       INTEGER NOT NULL DEFAULT 0 CHECK (late_minutes >= 0),
    status             TEXT NOT NULL CHECK (status IN (
                           'PRESENT','LATE','ABSENT','LEAVE','HOLIDAY','INCOMPLETE')),
    computed_from_events INTEGER NOT NULL DEFAULT 0 CHECK (computed_from_events >= 0),
    computed_at        TEXT NOT NULL,
    CONSTRAINT pk_workforce_attendance_days PRIMARY KEY (staff_id, work_date),
    CONSTRAINT ck_workforce_attendance_days_span CHECK (
        first_in_utc IS NULL OR last_out_utc IS NULL OR last_out_utc >= first_in_utc),
    CONSTRAINT fk_workforce_attendance_days_staff FOREIGN KEY (staff_id) REFERENCES workforce_staff (staff_id)
);

CREATE TABLE IF NOT EXISTS workforce_leave_requests (
    leave_request_id TEXT NOT NULL,
    agency_id        TEXT NOT NULL DEFAULT 'agency_default',
    staff_id         TEXT NOT NULL,
    leave_type       TEXT NOT NULL CHECK (leave_type IN (
                         'ANNUAL','SICK','UNPAID','MATERNITY','PATERNITY',
                         'BEREAVEMENT','HOLIDAY_REPLACEMENT')),
    start_date       TEXT NOT NULL CHECK (length(start_date) = 10),
    end_date         TEXT NOT NULL CHECK (length(end_date) = 10),
    days             INTEGER NOT NULL CHECK (days >= 1),
    status           TEXT NOT NULL DEFAULT 'REQUESTED' CHECK (status IN (
                         'REQUESTED','APPROVED','REJECTED','CANCELLED','TAKEN')),
    requested_at     TEXT NOT NULL,
    decided_by       TEXT,
    decided_at       TEXT,
    decision_reason  TEXT,
    idempotency_key  TEXT NOT NULL,
    CONSTRAINT pk_workforce_leave_requests PRIMARY KEY (leave_request_id),
    CONSTRAINT uq_workforce_leave_idem UNIQUE (agency_id, idempotency_key),
    CONSTRAINT ck_workforce_leave_dates CHECK (end_date >= start_date),
    -- A decided request names its decider and the day it was decided.
    CONSTRAINT ck_workforce_leave_decided CHECK (
        status = 'REQUESTED' OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)),
    CONSTRAINT ck_workforce_leave_rejection_reason CHECK (
        status <> 'REJECTED' OR (decision_reason IS NOT NULL AND length(decision_reason) >= 3)),
    CONSTRAINT fk_workforce_leave_staff FOREIGN KEY (staff_id) REFERENCES workforce_staff (staff_id)
);
CREATE INDEX IF NOT EXISTS ix_workforce_leave_staff_dates ON workforce_leave_requests (agency_id, staff_id, start_date);

-- ---------------------------------------------------------------------------
-- 6. Timesheet: the claim a person submits and a supervisor signs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workforce_timesheets (
    timesheet_id       TEXT NOT NULL,
    agency_id          TEXT NOT NULL DEFAULT 'agency_default',
    staff_id           TEXT NOT NULL,
    period_start       TEXT NOT NULL CHECK (length(period_start) = 10),
    period_end         TEXT NOT NULL CHECK (length(period_end) = 10),
    status             TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN (
                           'OPEN','SUBMITTED','APPROVED','REJECTED','LOCKED')),
    regular_minutes    INTEGER NOT NULL DEFAULT 0 CHECK (regular_minutes BETWEEN 0 AND 2400),
    overtime_minutes   INTEGER NOT NULL DEFAULT 0 CHECK (overtime_minutes BETWEEN 0 AND 1080),
    leave_days         INTEGER NOT NULL DEFAULT 0 CHECK (leave_days BETWEEN 0 AND 31),
    submitted_at       TEXT,
    approved_by        TEXT,
    approved_at        TEXT,
    rejected_reason    TEXT,
    -- Frozen digest of the approved content: a payslip cites it, so a later edit is visible.
    lock_hash          TEXT CHECK (lock_hash IS NULL OR length(lock_hash) = 64),
    version            INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    CONSTRAINT pk_workforce_timesheets PRIMARY KEY (timesheet_id),
    CONSTRAINT uq_workforce_timesheets_period UNIQUE (agency_id, staff_id, period_start),
    CONSTRAINT ck_workforce_timesheet_dates CHECK (period_end >= period_start),
    CONSTRAINT ck_workforce_timesheet_submitted CHECK (
        status IN ('OPEN','REJECTED') OR submitted_at IS NOT NULL),
    CONSTRAINT ck_workforce_timesheet_approved CHECK (
        status <> 'APPROVED' OR (approved_by IS NOT NULL AND approved_at IS NOT NULL
                                 AND lock_hash IS NOT NULL)),
    CONSTRAINT ck_workforce_timesheet_rejected CHECK (
        status <> 'REJECTED' OR (rejected_reason IS NOT NULL AND length(rejected_reason) >= 3)),
    CONSTRAINT fk_workforce_timesheets_staff FOREIGN KEY (staff_id) REFERENCES workforce_staff (staff_id)
);
CREATE INDEX IF NOT EXISTS ix_workforce_timesheets_status ON workforce_timesheets (agency_id, status, period_start);

-- ---------------------------------------------------------------------------
-- 7. Capacity-bound task assignment: replaces the free-text ops_tasks.assignee.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workforce_task_assignments (
    assignment_id   TEXT NOT NULL,
    agency_id       TEXT NOT NULL DEFAULT 'agency_default',
    task_id         TEXT NOT NULL,
    staff_id        TEXT NOT NULL,
    role_in_task    TEXT NOT NULL DEFAULT 'LEAD' CHECK (role_in_task IN ('LEAD','SUPPORT','REVIEWER')),
    planned_minutes INTEGER NOT NULL CHECK (planned_minutes BETWEEN 1 AND 480),
    status          TEXT NOT NULL DEFAULT 'ASSIGNED' CHECK (status IN (
                        'ASSIGNED','ACCEPTED','IN_PROGRESS','DONE','DROPPED')),
    assigned_by     TEXT NOT NULL CHECK (length(assigned_by) >= 3),
    assigned_at     TEXT NOT NULL,
    due_at_utc      TEXT,
    completed_at_utc TEXT,
    drop_reason     TEXT,
    idempotency_key TEXT NOT NULL,
    CONSTRAINT pk_workforce_task_assignments PRIMARY KEY (assignment_id),
    CONSTRAINT uq_workforce_assignment_once UNIQUE (task_id, staff_id, role_in_task),
    CONSTRAINT uq_workforce_assignment_idem UNIQUE (agency_id, idempotency_key),
    -- Done means done: the completion instant is not optional.
    CONSTRAINT ck_workforce_assignment_done CHECK (
        status <> 'DONE' OR completed_at_utc IS NOT NULL),
    CONSTRAINT ck_workforce_assignment_dropped CHECK (
        status <> 'DROPPED' OR (drop_reason IS NOT NULL AND length(drop_reason) >= 3)),
    CONSTRAINT fk_workforce_assignment_task FOREIGN KEY (task_id) REFERENCES ops_tasks (task_id),
    CONSTRAINT fk_workforce_assignment_staff FOREIGN KEY (staff_id) REFERENCES workforce_staff (staff_id)
);
CREATE INDEX IF NOT EXISTS ix_workforce_assignment_staff
    ON workforce_task_assignments (agency_id, staff_id, status, due_at_utc);
CREATE INDEX IF NOT EXISTS ix_workforce_assignment_task
    ON workforce_task_assignments (agency_id, task_id);

-- ---------------------------------------------------------------------------
-- 8. The payroll boundary: rates, runs, payslips, lines. Human-approved, integer money.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workforce_pay_rates (
    pay_rate_id       TEXT NOT NULL,
    agency_id         TEXT NOT NULL DEFAULT 'agency_default',
    staff_id          TEXT NOT NULL,
    component_code    TEXT NOT NULL CHECK (component_code IN (
                          'BASE_MONTHLY','BASE_DAILY','BASE_HOURLY','OVERTIME_HOURLY',
                          'MEAL_ALLOWANCE','TRANSPORT_ALLOWANCE','ATTENDANCE_BONUS')),
    unit              TEXT NOT NULL CHECK (unit IN ('MONTH','DAY','HOUR','PER_OCCURRENCE')),
    amount_minor      INTEGER NOT NULL CHECK (amount_minor > 0),
    currency          TEXT NOT NULL CHECK (length(currency) = 3),
    effective_from    TEXT NOT NULL,
    effective_to      TEXT,
    policy_version_id TEXT NOT NULL,
    approved_by       TEXT NOT NULL CHECK (length(approved_by) >= 3),
    approved_at       TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    CONSTRAINT pk_workforce_pay_rates PRIMARY KEY (pay_rate_id),
    CONSTRAINT uq_workforce_pay_rates UNIQUE (staff_id, component_code, effective_from),
    CONSTRAINT ck_workforce_pay_rates_window CHECK (effective_to IS NULL OR effective_to > effective_from),
    CONSTRAINT fk_workforce_pay_rates_staff FOREIGN KEY (staff_id) REFERENCES workforce_staff (staff_id),
    CONSTRAINT fk_workforce_pay_rates_policy FOREIGN KEY (policy_version_id)
        REFERENCES workforce_policy_versions (policy_version_id)
);
CREATE INDEX IF NOT EXISTS ix_workforce_pay_rates_lookup ON workforce_pay_rates (agency_id, staff_id, effective_from);

CREATE TABLE IF NOT EXISTS workforce_payroll_runs (
    payroll_run_id   TEXT NOT NULL,
    agency_id        TEXT NOT NULL DEFAULT 'agency_default',
    period_start     TEXT NOT NULL CHECK (length(period_start) = 10),
    period_end       TEXT NOT NULL CHECK (length(period_end) = 10),
    status           TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN (
                         'DRAFT','CALCULATED','APPROVED','PAID','VOID')),
    currency         TEXT NOT NULL CHECK (length(currency) = 3),
    total_gross_minor INTEGER NOT NULL DEFAULT 0 CHECK (total_gross_minor >= 0),
    total_net_minor  INTEGER NOT NULL DEFAULT 0 CHECK (total_net_minor >= 0),
    calculated_by    TEXT,
    calculated_at    TEXT,
    approved_by      TEXT,
    approved_at      TEXT,
    paid_at          TEXT,
    void_reason      TEXT,
    created_at       TEXT NOT NULL,
    CONSTRAINT pk_workforce_payroll_runs PRIMARY KEY (payroll_run_id),
    CONSTRAINT uq_workforce_payroll_period UNIQUE (agency_id, period_start, period_end),
    CONSTRAINT ck_workforce_payroll_dates CHECK (period_end >= period_start),
    CONSTRAINT ck_workforce_payroll_calculated CHECK (
        status IN ('DRAFT','VOID') OR (calculated_by IS NOT NULL AND calculated_at IS NOT NULL)),
    CONSTRAINT ck_workforce_payroll_approved CHECK (
        status NOT IN ('APPROVED','PAID') OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)),
    CONSTRAINT ck_workforce_payroll_void CHECK (
        status <> 'VOID' OR (void_reason IS NOT NULL AND length(void_reason) >= 3)),
    CONSTRAINT fk_workforce_payroll_agency FOREIGN KEY (agency_id) REFERENCES agencies (agency_id)
);

CREATE TABLE IF NOT EXISTS workforce_payslips (
    payslip_id      TEXT NOT NULL,
    agency_id       TEXT NOT NULL DEFAULT 'agency_default',
    payroll_run_id  TEXT NOT NULL,
    staff_id        TEXT NOT NULL,
    timesheet_id    TEXT,
    currency        TEXT NOT NULL CHECK (length(currency) = 3),
    gross_minor     INTEGER NOT NULL CHECK (gross_minor >= 0),
    deduction_minor INTEGER NOT NULL DEFAULT 0 CHECK (deduction_minor >= 0),
    net_minor       INTEGER NOT NULL CHECK (net_minor >= 0),
    worked_minutes  INTEGER NOT NULL DEFAULT 0 CHECK (worked_minutes >= 0),
    overtime_minutes INTEGER NOT NULL DEFAULT 0 CHECK (overtime_minutes >= 0),
    generated_at    TEXT NOT NULL,
    CONSTRAINT pk_workforce_payslips PRIMARY KEY (payslip_id),
    CONSTRAINT uq_workforce_payslips_once UNIQUE (payroll_run_id, staff_id),
    -- The arithmetic is a constraint, not a convention: a payslip cannot reach storage with
    -- net != gross - deduction, so no rounding drift can ever be stored silently.
    CONSTRAINT ck_workforce_payslip_arithmetic CHECK (net_minor = gross_minor - deduction_minor),
    CONSTRAINT fk_workforce_payslip_run FOREIGN KEY (payroll_run_id) REFERENCES workforce_payroll_runs (payroll_run_id),
    CONSTRAINT fk_workforce_payslip_staff FOREIGN KEY (staff_id) REFERENCES workforce_staff (staff_id),
    CONSTRAINT fk_workforce_payslip_timesheet FOREIGN KEY (timesheet_id) REFERENCES workforce_timesheets (timesheet_id)
);
CREATE INDEX IF NOT EXISTS ix_workforce_payslips_staff ON workforce_payslips (agency_id, staff_id, generated_at);

CREATE TABLE IF NOT EXISTS workforce_payslip_lines (
    payslip_line_id   TEXT NOT NULL,
    agency_id         TEXT NOT NULL DEFAULT 'agency_default',
    payslip_id        TEXT NOT NULL,
    component_code    TEXT NOT NULL CHECK (length(component_code) >= 3),
    kind              TEXT NOT NULL CHECK (kind IN (
                          'EARNING','DEDUCTION','EMPLOYER_CONTRIBUTION','TAX')),
    amount_minor      INTEGER NOT NULL CHECK (amount_minor >= 0),
    quantity_minutes  INTEGER CHECK (quantity_minutes IS NULL OR quantity_minutes >= 0),
    basis_note        TEXT NOT NULL CHECK (length(basis_note) >= 3),
    policy_version_id TEXT,
    created_at        TEXT NOT NULL,
    CONSTRAINT pk_workforce_payslip_lines PRIMARY KEY (payslip_line_id),
    CONSTRAINT uq_workforce_payslip_lines UNIQUE (payslip_id, component_code, kind),
    CONSTRAINT fk_workforce_payslip_line_slip FOREIGN KEY (payslip_id) REFERENCES workforce_payslips (payslip_id),
    CONSTRAINT fk_workforce_payslip_line_policy FOREIGN KEY (policy_version_id)
        REFERENCES workforce_policy_versions (policy_version_id)
);
CREATE INDEX IF NOT EXISTS ix_workforce_payslip_lines_slip ON workforce_payslip_lines (agency_id, payslip_id);
