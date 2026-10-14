-- A reserved-but-not-yet-confirmed live order. Must be committed before it is used (see 0004).
alter type public.trade_status add value if not exists 'pending';
