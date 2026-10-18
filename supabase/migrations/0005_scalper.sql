-- ForexAI Pro Scalper: live MT5 scalping engine integration.
--
-- Architecture: the Python engine (Windows VPS, service-role key, outbound-only) is the ONLY writer of
-- engine state and the ONLY component that can place orders. The browser reads state through RLS-gated
-- tables (Supabase Realtime) and issues commands through SECURITY DEFINER RPCs that re-check the
-- 'auto_trading' module purchase. Nothing here touches the existing payment/subscription tables.

-- ---------- configuration ----------
create table public.scalper_config (
  user_id uuid primary key references auth.users(id) on delete cascade,
  config jsonb not null,
  version int not null default 1,
  updated_by uuid references auth.users(id),
  updated_at timestamptz not null default now(),
  constraint scalper_config_is_object check (jsonb_typeof(config) = 'object')
);

-- ---------- live engine state (one row per user, written only by the engine) ----------
create table public.scalper_state (
  user_id uuid primary key references auth.users(id) on delete cascade,
  mode text not null default 'DISCONNECTED'
    check (mode in ('DISCONNECTED', 'PAPER', 'LIVE ARMED', 'LIVE ACTIVE', 'PAUSED', 'ERROR')),
  heartbeat_at timestamptz,
  mt5_connected boolean not null default false,
  last_mt5_heartbeat_at timestamptz,
  trading_locked boolean not null default false,
  lock_reason text,
  analysis_running boolean not null default false,
  entries_enabled boolean not null default false,
  paused boolean not null default true,
  exec_mode text not null default 'none' check (exec_mode in ('none', 'paper', 'live')),
  account jsonb not null default '{}'::jsonb,        -- login, server, type, currency, balance, equity, free margin
  quotes jsonb not null default '{}'::jsonb,         -- per instrument: bid, ask, spread, tick time
  analysis jsonb not null default '{}'::jsonb,       -- latest structured analysis per instrument
  health jsonb not null default '{}'::jsonb,         -- named checks + warnings
  stats jsonb not null default '{}'::jsonb,          -- daily P&L, win rate, profit factor, drawdown
  live_preview jsonb,                                -- what the user must see before arming live
  symbol_candidates jsonb not null default '{}'::jsonb,
  next_eval_at timestamptz,
  last_analysis_at timestamptz,
  engine_version text,
  started_at timestamptz,
  updated_at timestamptz not null default now()
);

-- ---------- commands (dashboard -> engine) ----------
create table public.scalper_commands (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  command text not null,
  payload jsonb not null default '{}'::jsonb,
  status text not null default 'pending' check (status in ('pending', 'processing', 'done', 'rejected', 'failed')),
  result jsonb,
  created_at timestamptz not null default now(),
  processed_at timestamptz
);
create index scalper_commands_pending on public.scalper_commands (user_id, status, created_at);

-- ---------- signals / analyses ----------
create table public.scalper_signals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  signal_id text not null,                            -- deterministic: hash(instrument, bar time, strategy version, direction)
  instrument text not null,
  broker_symbol text not null,
  bar_time timestamptz not null,
  decision text not null check (decision in ('BUY', 'SELL', 'NO TRADE')),
  outcome text not null default 'analysis'
    check (outcome in ('analysis', 'rejected', 'paper_executed', 'live_executed', 'expired', 'failed')),
  reject_reason text,
  analysis jsonb not null,
  created_at timestamptz not null default now(),
  unique (user_id, signal_id)                         -- a candle can never produce a second entry
);
create index scalper_signals_user_time on public.scalper_signals (user_id, created_at desc);

-- ---------- executions (one per signal and mode; the durable order state machine) ----------
create table public.scalper_executions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  signal_id text not null,
  mode text not null check (mode in ('paper', 'live')),
  state text not null,                                -- INTENT, SENT, FILLED, PROTECTED, REJECTED, AMBIGUOUS, UNFILLED, FAILED
  instrument text not null,
  broker_symbol text not null,
  side text not null check (side in ('BUY', 'SELL')),
  requested_volume numeric(18,8) not null,
  filled_volume numeric(18,8),
  requested_price numeric(18,8),
  fill_price numeric(18,8),
  sl numeric(18,8),
  tp numeric(18,8),
  retcode int,
  order_ticket bigint,
  deal_ticket bigint,
  position_ticket bigint,
  protective_state text,                              -- OK, MISSING, RECOVERED, CLOSED_UNPROTECTED
  error text,
  detail jsonb not null default '{}'::jsonb,          -- sanitized request/result snapshots (never credentials)
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, signal_id, mode)
);

-- ---------- positions (bot-managed; includes trailing-stop recovery state) ----------
create table public.scalper_positions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  mode text not null check (mode in ('paper', 'live')),
  ticket bigint not null,                             -- broker position identifier (synthetic for paper)
  signal_id text,
  instrument text not null,
  broker_symbol text not null,
  side text not null check (side in ('BUY', 'SELL')),
  volume numeric(18,8) not null,
  entry_price numeric(18,8) not null,                 -- actual fill price
  sl numeric(18,8),
  tp numeric(18,8),
  original_sl numeric(18,8),
  original_risk numeric(18,4),                        -- account currency, at entry
  high_water numeric(18,8),                           -- BUY: highest bid since entry
  low_water numeric(18,8),                            -- SELL: lowest ask since entry
  trail_active boolean not null default false,
  breakeven_done boolean not null default false,
  last_ack_sl numeric(18,8),                          -- last stop the broker acknowledged
  trail_mode text,
  current_price numeric(18,8),
  floating_pnl numeric(18,4),
  status text not null default 'open' check (status in ('open', 'closed')),
  close_price numeric(18,8),
  pnl numeric(18,4),                                  -- realized, after commission/swap/fees
  fees numeric(18,4),
  close_reason text,
  meta jsonb not null default '{}'::jsonb,           -- entry ATR, spec snapshot, adoption notes
  opened_at timestamptz not null default now(),
  closed_at timestamptz,
  updated_at timestamptz not null default now(),
  unique (user_id, mode, ticket)
);
create index scalper_positions_user_status on public.scalper_positions (user_id, status, opened_at desc);

-- ---------- risk events / errors ----------
create table public.scalper_risk_events (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  severity text not null check (severity in ('info', 'warn', 'critical')),
  kind text not null,
  message text not null,
  details jsonb not null default '{}'::jsonb,
  resolved_at timestamptz,
  created_at timestamptz not null default now()
);
create index scalper_risk_events_user_time on public.scalper_risk_events (user_id, created_at desc);

-- ---------- tamper-evident audit log (hash chain, append-only) ----------
create table public.scalper_audit (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  actor text not null,                                -- 'user' | 'engine' | 'system'
  action text not null,
  details jsonb not null default '{}'::jsonb,
  prev_hash text not null default '',
  hash text not null default '',
  created_at timestamptz not null default now()
);
create index scalper_audit_user on public.scalper_audit (user_id, id desc);

create or replace function public.scalper_audit_hash(_prev text, _user uuid, _actor text, _action text, _details jsonb, _at timestamptz)
returns text language sql immutable set search_path = public as $$
  select encode(sha256(convert_to(
    _prev || '|' || _user::text || '|' || _actor || '|' || _action || '|' || _details::text || '|' ||
    to_char(_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'), 'UTF8')), 'hex')
$$;

create or replace function public.scalper_audit_chain() returns trigger
language plpgsql set search_path = public as $$
declare prev text;
begin
  perform pg_advisory_xact_lock(hashtextextended(new.user_id::text, 5));
  select hash into prev from public.scalper_audit where user_id = new.user_id order by id desc limit 1;
  new.created_at := clock_timestamp();
  new.prev_hash := coalesce(prev, repeat('0', 64));
  new.hash := public.scalper_audit_hash(new.prev_hash, new.user_id, new.actor, new.action, new.details, new.created_at);
  return new;
end $$;

create trigger scalper_audit_chain_trg before insert on public.scalper_audit
  for each row execute function public.scalper_audit_chain();

create or replace function public.scalper_audit_immutable() returns trigger
language plpgsql set search_path = public as $$
begin
  -- A cascade from deleting the auth user runs one trigger level deeper than a direct DELETE.
  if tg_op = 'DELETE' and pg_trigger_depth() > 1 then return old; end if;
  raise exception 'scalper_audit is append-only';
end $$;

create trigger scalper_audit_no_update before update or delete on public.scalper_audit
  for each row execute function public.scalper_audit_immutable();
create trigger scalper_audit_no_truncate before truncate on public.scalper_audit
  for each statement execute function public.scalper_audit_immutable();

create or replace function public.scalper_audit_append(_user uuid, _actor text, _action text, _details jsonb)
returns void language sql security definer set search_path = public as $$
  insert into public.scalper_audit (user_id, actor, action, details) values (_user, _actor, _action, coalesce(_details, '{}'::jsonb));
$$;
revoke all on function public.scalper_audit_append(uuid, text, text, jsonb) from public, anon, authenticated;

-- Recomputes the chain; false means a row was altered or removed.
create or replace function public.scalper_verify_audit_chain(_user uuid default null)
returns boolean language plpgsql security definer set search_path = public as $$
declare uid uuid := coalesce(_user, auth.uid()); r record; expected_prev text := repeat('0', 64); h text;
begin
  if uid is null then raise exception 'forbidden'; end if;
  if uid <> auth.uid() and not public.has_role(auth.uid(), 'admin') then raise exception 'forbidden'; end if;
  for r in select * from public.scalper_audit where user_id = uid order by id loop
    if r.prev_hash <> expected_prev then return false; end if;
    h := public.scalper_audit_hash(r.prev_hash, r.user_id, r.actor, r.action, r.details, r.created_at);
    if h <> r.hash then return false; end if;
    expected_prev := r.hash;
  end loop;
  return true;
end $$;
revoke execute on function public.scalper_verify_audit_chain(uuid) from public, anon;
grant execute on function public.scalper_verify_audit_chain(uuid) to authenticated;

-- ---------- RLS: owners with the Automated Trading module can read; nobody writes directly ----------
alter table public.scalper_config enable row level security;
alter table public.scalper_state enable row level security;
alter table public.scalper_commands enable row level security;
alter table public.scalper_signals enable row level security;
alter table public.scalper_executions enable row level security;
alter table public.scalper_positions enable row level security;
alter table public.scalper_risk_events enable row level security;
alter table public.scalper_audit enable row level security;

create policy "scalper config read" on public.scalper_config for select to authenticated
  using (user_id = auth.uid() and public.has_module(auth.uid(), 'auto_trading'));
create policy "scalper state read" on public.scalper_state for select to authenticated
  using (user_id = auth.uid() and public.has_module(auth.uid(), 'auto_trading'));
create policy "scalper commands read" on public.scalper_commands for select to authenticated
  using (user_id = auth.uid() and public.has_module(auth.uid(), 'auto_trading'));
create policy "scalper signals read" on public.scalper_signals for select to authenticated
  using (user_id = auth.uid() and public.has_module(auth.uid(), 'auto_trading'));
create policy "scalper executions read" on public.scalper_executions for select to authenticated
  using (user_id = auth.uid() and public.has_module(auth.uid(), 'auto_trading'));
create policy "scalper positions read" on public.scalper_positions for select to authenticated
  using (user_id = auth.uid() and public.has_module(auth.uid(), 'auto_trading'));
create policy "scalper risk events read" on public.scalper_risk_events for select to authenticated
  using (user_id = auth.uid() and public.has_module(auth.uid(), 'auto_trading'));
create policy "scalper audit read" on public.scalper_audit for select to authenticated
  using (user_id = auth.uid() and public.has_module(auth.uid(), 'auto_trading'));

-- ---------- RPC: save configuration ----------
-- The engine is the authority on validity (it also enforces hard caps from its own environment);
-- this guard rejects obviously unsafe values before they are stored.
create or replace function public.scalper_save_config(_config jsonb)
returns int language plpgsql security definer set search_path = public as $$
declare
  uid uuid := auth.uid();
  old jsonb;
  risk jsonb := _config -> 'risk';
  inst text;
  new_ver int;
  f text;
begin
  if uid is null or not public.has_module(uid, 'auto_trading') then raise exception 'Automated Trading is not activated'; end if;
  if jsonb_typeof(_config) <> 'object' or pg_column_size(_config) > 32768 then raise exception 'invalid configuration'; end if;
  if risk is null or jsonb_typeof(risk) <> 'object' then raise exception 'risk section is required'; end if;
  if jsonb_typeof(_config -> 'instruments') <> 'object' then raise exception 'instruments section is required'; end if;

  -- numeric range guards on the settings that bound losses
  if coalesce((risk->>'risk_per_trade_pct')::numeric, -1) not between 0.01 and 2 then raise exception 'risk_per_trade_pct must be 0.01 to 2'; end if;
  if coalesce((risk->>'max_daily_loss_pct')::numeric, -1) not between 0.1 and 10 then raise exception 'max_daily_loss_pct must be 0.1 to 10'; end if;
  if coalesce((risk->>'max_aggregate_open_risk_pct')::numeric, -1) not between 0.01 and 5 then raise exception 'max_aggregate_open_risk_pct must be 0.01 to 5'; end if;
  if coalesce((risk->>'max_total_positions')::int, -1) not between 1 and 10 then raise exception 'max_total_positions must be 1 to 10'; end if;
  if coalesce((risk->>'max_positions_per_symbol')::int, -1) not between 1 and 5 then raise exception 'max_positions_per_symbol must be 1 to 5'; end if;
  if coalesce((risk->>'max_consecutive_losses')::int, -1) not between 1 and 20 then raise exception 'max_consecutive_losses must be 1 to 20'; end if;
  if coalesce((risk->>'max_margin_utilization_pct')::numeric, -1) not between 1 and 80 then raise exception 'max_margin_utilization_pct must be 1 to 80'; end if;
  if coalesce((risk->>'min_net_rr')::numeric, -1) not between 0.5 and 10 then raise exception 'min_net_rr must be 0.5 to 10'; end if;
  if coalesce(risk->>'breach_policy', '') not in ('pause', 'close_all') then raise exception 'breach_policy must be pause or close_all'; end if;

  select config into old from public.scalper_config where user_id = uid for update;

  -- Instrument-to-broker-symbol mapping is admin-controlled.
  if old is not null and not public.has_role(uid, 'admin') then
    for inst in select jsonb_object_keys(_config -> 'instruments') loop
      if coalesce(_config -> 'instruments' -> inst ->> 'broker_symbol', '') is distinct from coalesce(old -> 'instruments' -> inst ->> 'broker_symbol', '') then
        raise exception 'Only an admin can change the broker symbol mapping';
      end if;
    end loop;
  end if;
  if old is null and not public.has_role(uid, 'admin') then
    raise exception 'The engine has not created the default configuration yet';
  end if;

  update public.scalper_config
     set config = _config, version = version + 1, updated_by = uid, updated_at = now()
   where user_id = uid
  returning version into new_ver;
  if not found then
    insert into public.scalper_config (user_id, config, updated_by) values (uid, _config, uid) returning version into new_ver;
  end if;

  perform public.scalper_audit_append(uid, 'user', 'config.save', jsonb_build_object('version', new_ver, 'risk', risk));
  perform public.write_audit('scalper.config', 'scalper_config', uid::text, jsonb_build_object('version', new_ver));
  return new_ver;
end $$;

-- ---------- RPC: send a command to the engine ----------
create or replace function public.scalper_send_command(_command text, _payload jsonb default '{}'::jsonb)
returns uuid language plpgsql security definer set search_path = public as $$
declare uid uuid := auth.uid(); cid uuid; recent int;
begin
  if uid is null or not public.has_module(uid, 'auto_trading') then raise exception 'Automated Trading is not activated'; end if;
  if _command not in ('connect', 'discover_symbols', 'start_analysis', 'stop_analysis', 'start_paper', 'request_live_preview',
                      'arm_live', 'disarm_live', 'pause_entries', 'resume_entries', 'emergency_stop',
                      'close_positions', 'unlock_trading', 'reconcile') then
    raise exception 'unknown command';
  end if;
  if jsonb_typeof(coalesce(_payload, '{}'::jsonb)) <> 'object' or pg_column_size(coalesce(_payload, '{}'::jsonb)) > 4096 then
    raise exception 'invalid payload';
  end if;
  select count(*) into recent from public.scalper_commands where user_id = uid and created_at > now() - interval '1 minute';
  if recent >= 30 then raise exception 'Too many commands, slow down'; end if;

  if _command = 'arm_live' and (coalesce(_payload->>'confirm_text', '') <> 'ARM LIVE' or coalesce(_payload->>'preview_token', '') = '') then
    raise exception 'Arming live trading needs the preview token and the typed confirmation ARM LIVE';
  end if;
  if _command = 'close_positions' and coalesce(_payload->>'confirm_text', '') <> 'CLOSE' then
    raise exception 'Closing positions needs the typed confirmation CLOSE';
  end if;
  if _command = 'unlock_trading' and coalesce(_payload->>'confirm_text', '') <> 'UNLOCK' then
    raise exception 'Unlocking trading needs the typed confirmation UNLOCK';
  end if;

  insert into public.scalper_commands (user_id, command, payload)
  values (uid, _command, coalesce(_payload, '{}'::jsonb)) returning id into cid;

  perform public.scalper_audit_append(uid, 'user', 'command.' || _command,
    jsonb_build_object('command_id', cid, 'payload', coalesce(_payload, '{}'::jsonb) - 'preview_token'));
  perform public.write_audit('scalper.command', 'scalper_commands', cid::text, jsonb_build_object('command', _command));
  return cid;
end $$;

revoke execute on function public.scalper_save_config(jsonb) from public, anon;
revoke execute on function public.scalper_send_command(text, jsonb) from public, anon;
grant execute on function public.scalper_save_config(jsonb) to authenticated;
grant execute on function public.scalper_send_command(text, jsonb) to authenticated;

-- ---------- Realtime (RLS applies to the change stream) ----------
do $$
declare t text;
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    foreach t in array array['scalper_state', 'scalper_positions', 'scalper_signals', 'scalper_commands', 'scalper_risk_events'] loop
      begin
        execute format('alter publication supabase_realtime add table public.%I', t);
      exception when duplicate_object then null;
      end;
    end loop;
  end if;
end $$;
