-- ForexAI Pro core schema: roles, payment settings, purchases, module access, audit logs.
-- All privileged changes go through SECURITY DEFINER functions that check the caller's role.

create extension if not exists pgcrypto;

create type public.app_role as enum ('admin', 'user');
create type public.module_key as enum ('ai_analysis', 'auto_trading');
create type public.payment_status as enum ('pending', 'verified', 'rejected');
create type public.access_status as enum ('active', 'revoked');

-- ---------- profiles & roles ----------
create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text not null,
  full_name text,
  created_at timestamptz not null default now()
);

create table public.user_roles (
  user_id uuid not null references auth.users(id) on delete cascade,
  role public.app_role not null,
  primary key (user_id, role)
);

create or replace function public.has_role(_user uuid, _role public.app_role)
returns boolean
language sql stable security definer set search_path = public
as $$ select exists (select 1 from public.user_roles where user_id = _user and role = _role) $$;

create or replace function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = public
as $$
begin
  insert into public.profiles (id, email, full_name)
  values (new.id, new.email, coalesce(new.raw_user_meta_data->>'full_name', ''));
  insert into public.user_roles (user_id, role) values (new.id, 'user');
  return new;
end $$;

create trigger on_auth_user_created
after insert on auth.users
for each row execute function public.handle_new_user();

-- ---------- payment settings (one row per module) ----------
create table public.payment_settings (
  module public.module_key primary key,
  binance_url text not null default '',
  price_usd numeric(10,2) not null default 200.00,
  updated_by uuid references auth.users(id),
  updated_at timestamptz not null default now(),
  constraint binance_url_https check (binance_url = '' or binance_url ~* '^https://')
);
insert into public.payment_settings (module) values ('ai_analysis'), ('auto_trading');

-- ---------- payment submissions ----------
create table public.payment_submissions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  module public.module_key not null,
  tx_id text not null check (length(trim(tx_id)) between 6 and 128),
  proof_path text,
  note text,
  status public.payment_status not null default 'pending',
  verified_amount_usd numeric(10,2),
  admin_note text,
  reviewed_by uuid references auth.users(id),
  reviewed_at timestamptz,
  created_at timestamptz not null default now()
);
-- A transaction ID can back only one non-rejected submission (stops reuse across users/modules).
create unique index payment_tx_unique on public.payment_submissions (lower(trim(tx_id))) where status <> 'rejected';
-- One open submission per user per module.
create unique index payment_one_pending on public.payment_submissions (user_id, module) where status = 'pending';

-- ---------- module access ----------
create table public.module_access (
  user_id uuid not null references auth.users(id) on delete cascade,
  module public.module_key not null,
  status public.access_status not null default 'active',
  source_submission uuid references public.payment_submissions(id),
  activated_by uuid references auth.users(id),
  activated_at timestamptz not null default now(),
  revoked_at timestamptz,
  revoke_reason text,
  primary key (user_id, module)
);

-- ---------- audit log ----------
create table public.audit_logs (
  id bigint generated always as identity primary key,
  actor_id uuid references auth.users(id),
  action text not null,
  entity text not null,
  entity_id text,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create or replace function public.write_audit(_action text, _entity text, _entity_id text, _details jsonb)
returns void language sql security definer set search_path = public
as $$ insert into public.audit_logs (actor_id, action, entity, entity_id, details) values (auth.uid(), _action, _entity, _entity_id, coalesce(_details, '{}'::jsonb)) $$;
revoke all on function public.write_audit(text, text, text, jsonb) from public, anon, authenticated;

-- ---------- RLS ----------
alter table public.profiles enable row level security;
alter table public.user_roles enable row level security;
alter table public.payment_settings enable row level security;
alter table public.payment_submissions enable row level security;
alter table public.module_access enable row level security;
alter table public.audit_logs enable row level security;

create policy "profiles self or admin read" on public.profiles for select to authenticated
  using (id = auth.uid() or public.has_role(auth.uid(), 'admin'));
create policy "profiles self update name" on public.profiles for update to authenticated
  using (id = auth.uid()) with check (id = auth.uid());

create policy "roles self or admin read" on public.user_roles for select to authenticated
  using (user_id = auth.uid() or public.has_role(auth.uid(), 'admin'));
-- no insert/update/delete policies: roles change only via SQL or admin functions.

create policy "payment settings read" on public.payment_settings for select to authenticated using (true);
-- writes only via admin_update_payment_settings()

create policy "submissions own or admin read" on public.payment_submissions for select to authenticated
  using (user_id = auth.uid() or public.has_role(auth.uid(), 'admin'));
create policy "submissions own insert pending" on public.payment_submissions for insert to authenticated
  with check (user_id = auth.uid() and status = 'pending' and reviewed_by is null and verified_amount_usd is null);

create policy "access own or admin read" on public.module_access for select to authenticated
  using (user_id = auth.uid() or public.has_role(auth.uid(), 'admin'));

create policy "audit admin read" on public.audit_logs for select to authenticated
  using (public.has_role(auth.uid(), 'admin'));

-- ---------- admin functions ----------
create or replace function public.admin_update_payment_settings(_module public.module_key, _url text, _price numeric default null)
returns void language plpgsql security definer set search_path = public
as $$
begin
  if not public.has_role(auth.uid(), 'admin') then raise exception 'forbidden'; end if;
  if _url <> '' and _url !~* '^https://' then raise exception 'URL must start with https://'; end if;
  update public.payment_settings
     set binance_url = _url, price_usd = coalesce(_price, price_usd), updated_by = auth.uid(), updated_at = now()
   where module = _module;
  perform public.write_audit('payment_settings.update', 'payment_settings', _module::text, jsonb_build_object('url', _url, 'price', _price));
end $$;

-- Approve or reject. Approval requires the admin to attest they confirmed the payment on Binance
-- and to enter the amount actually received; it must meet the module price.
create or replace function public.admin_review_payment(
  _submission uuid, _approve boolean, _verified_amount numeric default null, _note text default null)
returns void language plpgsql security definer set search_path = public
as $$
declare s public.payment_submissions; price numeric;
begin
  if not public.has_role(auth.uid(), 'admin') then raise exception 'forbidden'; end if;
  select * into s from public.payment_submissions where id = _submission for update;
  if not found then raise exception 'submission not found'; end if;
  if s.status <> 'pending' then raise exception 'submission already reviewed'; end if;

  if _approve then
    select price_usd into price from public.payment_settings where module = s.module;
    if _verified_amount is null or _verified_amount < price then
      raise exception 'verified amount must be at least %', price;
    end if;
    update public.payment_submissions
       set status='verified', verified_amount_usd=_verified_amount, admin_note=_note, reviewed_by=auth.uid(), reviewed_at=now()
     where id = s.id;
    insert into public.module_access (user_id, module, status, source_submission, activated_by)
    values (s.user_id, s.module, 'active', s.id, auth.uid())
    on conflict (user_id, module) do update
      set status='active', source_submission=excluded.source_submission, activated_by=excluded.activated_by,
          activated_at=now(), revoked_at=null, revoke_reason=null;
    perform public.write_audit('payment.verify', 'payment_submissions', s.id::text,
      jsonb_build_object('user', s.user_id, 'module', s.module, 'amount', _verified_amount));
  else
    update public.payment_submissions
       set status='rejected', admin_note=_note, reviewed_by=auth.uid(), reviewed_at=now()
     where id = s.id;
    perform public.write_audit('payment.reject', 'payment_submissions', s.id::text,
      jsonb_build_object('user', s.user_id, 'module', s.module, 'note', _note));
  end if;
end $$;

create or replace function public.admin_set_module_access(_user uuid, _module public.module_key, _active boolean, _reason text default null)
returns void language plpgsql security definer set search_path = public
as $$
begin
  if not public.has_role(auth.uid(), 'admin') then raise exception 'forbidden'; end if;
  if _active then
    insert into public.module_access (user_id, module, status, activated_by)
    values (_user, _module, 'active', auth.uid())
    on conflict (user_id, module) do update
      set status='active', activated_by=auth.uid(), activated_at=now(), revoked_at=null, revoke_reason=null;
  else
    update public.module_access set status='revoked', revoked_at=now(), revoke_reason=_reason
     where user_id=_user and module=_module;
  end if;
  perform public.write_audit(case when _active then 'access.activate' else 'access.revoke' end,
    'module_access', _user::text, jsonb_build_object('module', _module, 'reason', _reason));
end $$;

create or replace function public.admin_set_role(_user uuid, _role public.app_role, _grant boolean)
returns void language plpgsql security definer set search_path = public
as $$
begin
  if not public.has_role(auth.uid(), 'admin') then raise exception 'forbidden'; end if;
  if _user = auth.uid() and not _grant and _role = 'admin' then raise exception 'cannot remove your own admin role'; end if;
  if _grant then insert into public.user_roles values (_user, _role) on conflict do nothing;
  else delete from public.user_roles where user_id=_user and role=_role; end if;
  perform public.write_audit('role.change', 'user_roles', _user::text, jsonb_build_object('role', _role, 'grant', _grant));
end $$;

grant execute on function public.has_role(uuid, public.app_role) to authenticated;
grant execute on function public.admin_update_payment_settings(public.module_key, text, numeric) to authenticated;
grant execute on function public.admin_review_payment(uuid, boolean, numeric, text) to authenticated;
grant execute on function public.admin_set_module_access(uuid, public.module_key, boolean, text) to authenticated;
grant execute on function public.admin_set_role(uuid, public.app_role, boolean) to authenticated;
revoke execute on function public.handle_new_user() from public, anon, authenticated;

-- ---------- storage for payment proofs (private) ----------
insert into storage.buckets (id, name, public) values ('payment-proofs', 'payment-proofs', false)
on conflict (id) do nothing;

create policy "proof upload own folder" on storage.objects for insert to authenticated
  with check (bucket_id = 'payment-proofs' and (storage.foldername(name))[1] = auth.uid()::text);
create policy "proof read own or admin" on storage.objects for select to authenticated
  using (bucket_id = 'payment-proofs' and ((storage.foldername(name))[1] = auth.uid()::text or public.has_role(auth.uid(), 'admin')));
