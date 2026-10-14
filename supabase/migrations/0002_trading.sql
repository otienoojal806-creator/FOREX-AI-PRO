-- Trading layer: signals, automation settings, trades, broker credentials.
-- Reads are RLS-gated by an ACTIVE module purchase. Writes happen only through
-- SECURITY DEFINER RPCs or edge functions (service role) that re-check access.

create type public.trade_mode as enum ('simulated', 'live');
create type public.trade_status as enum ('open', 'closed');

create or replace function public.has_module(_user uuid, _module public.module_key)
returns boolean language sql stable security definer set search_path = public
as $$ select exists (select 1 from public.module_access where user_id = _user and module = _module and status = 'active') $$;
revoke execute on function public.has_module(uuid, public.module_key) from public, anon;
grant execute on function public.has_module(uuid, public.module_key) to authenticated;

create table public.signals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  symbol text not null,
  interval text not null,
  direction text not null check (direction in ('BUY', 'SELL')),
  entry numeric(14,5) not null,
  tp numeric(14,5) not null,
  sl numeric(14,5) not null,
  confidence int not null check (confidence between 0 and 100),
  rationale jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);
create index signals_user_time on public.signals (user_id, created_at desc);

create table public.automation_settings (
  user_id uuid primary key references auth.users(id) on delete cascade,
  mode public.trade_mode not null default 'simulated',
  lot_size numeric(8,2) not null default 0.01 check (lot_size between 0.01 and 100),
  risk_pct numeric(5,2) not null default 1 check (risk_pct between 0.1 and 10),
  sl_pips int not null default 30 check (sl_pips between 1 and 2000),
  tp_pips int not null default 60 check (tp_pips between 1 and 5000),
  trailing_pips int not null default 0 check (trailing_pips between 0 and 2000),
  max_open_trades int not null default 3 check (max_open_trades between 1 and 20),
  min_confidence int not null default 70 check (min_confidence between 50 and 99),
  symbols text[] not null default '{EUR/USD}',
  auto_enabled boolean not null default false,
  live_consent_at timestamptz,
  broker_connected boolean not null default false,
  updated_at timestamptz not null default now(),
  -- Live mode is impossible without recorded consent AND a connected broker.
  constraint live_requires_consent_and_broker check (mode = 'simulated' or (live_consent_at is not null and broker_connected))
);

create table public.trades (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  mode public.trade_mode not null,
  symbol text not null,
  direction text not null check (direction in ('BUY', 'SELL')),
  lots numeric(8,2) not null,
  entry_price numeric(14,5) not null,
  sl numeric(14,5) not null,
  tp numeric(14,5) not null,
  trailing_pips int not null default 0,
  status public.trade_status not null default 'open',
  close_price numeric(14,5),
  pnl_usd numeric(14,2),
  close_reason text,
  broker_position_id text,
  signal_id uuid references public.signals(id),
  opened_at timestamptz not null default now(),
  closed_at timestamptz
);
create index trades_user_status on public.trades (user_id, status, opened_at desc);

-- Broker credentials: RLS on, NO policies => unreadable by clients. Edge functions (service role) only.
create table public.broker_credentials (
  user_id uuid primary key references auth.users(id) on delete cascade,
  provider text not null default 'metaapi',
  region text not null default 'new-york',
  account_id text not null,
  token_enc text not null,
  created_at timestamptz not null default now()
);

alter table public.signals enable row level security;
alter table public.automation_settings enable row level security;
alter table public.trades enable row level security;
alter table public.broker_credentials enable row level security;

create policy "signals own with module" on public.signals for select to authenticated
  using (user_id = auth.uid() and public.has_module(auth.uid(), 'ai_analysis'));
create policy "settings own with module" on public.automation_settings for select to authenticated
  using (user_id = auth.uid() and public.has_module(auth.uid(), 'auto_trading'));
create policy "trades own with module" on public.trades for select to authenticated
  using (user_id = auth.uid() and public.has_module(auth.uid(), 'auto_trading'));

create or replace function public.save_automation_settings(
  _lot numeric, _risk numeric, _sl int, _tp int, _trailing int, _max_open int,
  _min_conf int, _symbols text[], _mode public.trade_mode, _auto boolean)
returns void language plpgsql security definer set search_path = public
as $$
declare uid uuid := auth.uid();
begin
  if uid is null or not public.has_module(uid, 'auto_trading') then raise exception 'Automated Trading is not activated'; end if;
  insert into public.automation_settings (user_id) values (uid) on conflict do nothing;
  update public.automation_settings set
    lot_size = _lot, risk_pct = _risk, sl_pips = _sl, tp_pips = _tp, trailing_pips = _trailing,
    max_open_trades = _max_open, min_confidence = _min_conf, symbols = _symbols,
    mode = _mode, auto_enabled = _auto, updated_at = now()
  where user_id = uid;
  perform public.write_audit('automation.settings', 'automation_settings', uid::text,
    jsonb_build_object('mode', _mode, 'auto', _auto, 'lot', _lot, 'risk_pct', _risk));
exception when check_violation then
  raise exception 'Live mode requires a connected broker and recorded consent';
end $$;

create or replace function public.set_live_consent(_give boolean)
returns void language plpgsql security definer set search_path = public
as $$
declare uid uuid := auth.uid();
begin
  if uid is null or not public.has_module(uid, 'auto_trading') then raise exception 'Automated Trading is not activated'; end if;
  insert into public.automation_settings (user_id) values (uid) on conflict do nothing;
  if _give then
    update public.automation_settings set live_consent_at = now(), updated_at = now() where user_id = uid;
  else
    update public.automation_settings set live_consent_at = null, mode = 'simulated', auto_enabled = false, updated_at = now() where user_id = uid;
  end if;
  perform public.write_audit(case when _give then 'live.consent_given' else 'live.consent_revoked' end, 'automation_settings', uid::text, '{}'::jsonb);
end $$;

revoke execute on function public.save_automation_settings(numeric, numeric, int, int, int, int, int, text[], public.trade_mode, boolean) from public, anon;
revoke execute on function public.set_live_consent(boolean) from public, anon;
grant execute on function public.save_automation_settings(numeric, numeric, int, int, int, int, int, text[], public.trade_mode, boolean) to authenticated;
grant execute on function public.set_live_consent(boolean) to authenticated;
