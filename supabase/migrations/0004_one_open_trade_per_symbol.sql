-- Race protection: at most one pending/open trade per user and symbol, enforced by the database,
-- so simultaneous requests (e.g. two browser tabs) cannot both place a live order.
create unique index if not exists trades_one_active_per_symbol
  on public.trades (user_id, symbol) where status in ('open', 'pending');
