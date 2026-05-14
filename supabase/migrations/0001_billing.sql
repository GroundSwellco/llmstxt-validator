-- 0001_billing.sql
-- LLMs.txt Validator — billing + tier scaffolding.
-- Run this once in Supabase SQL Editor.

create table if not exists public.profiles (
    id uuid primary key references auth.users(id) on delete cascade,
    tier text not null default 'free' check (tier in ('free', 'business', 'agency')),
    stripe_customer_id text unique,
    stripe_subscription_id text unique,
    stripe_price_id text,
    subscription_status text,
    current_period_end timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists profiles_stripe_customer_id_idx
    on public.profiles (stripe_customer_id);

-- Auto-create a profile row for every new auth.users row.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.profiles (id) values (new.id)
        on conflict (id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

-- Backfill: ensure any users that signed up before this migration get a profile.
insert into public.profiles (id)
    select id from auth.users
    on conflict (id) do nothing;

-- Keep updated_at fresh.
create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists profiles_set_updated_at on public.profiles;
create trigger profiles_set_updated_at
    before update on public.profiles
    for each row execute function public.touch_updated_at();

-- Row Level Security.
alter table public.profiles enable row level security;

drop policy if exists "Users can view their own profile" on public.profiles;
create policy "Users can view their own profile"
    on public.profiles for select
    using (auth.uid() = id);

-- No insert/update policies for authenticated users — the FastAPI backend
-- writes profiles using the service_role key, which bypasses RLS.
