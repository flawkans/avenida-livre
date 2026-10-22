-- Avenida Livre · tabelas do login (rode uma vez no Supabase: SQL Editor > New query > colar > Run)

-- Perfil de cada jogador: apelido único + o resto do progresso (time, visual, cartazes, missões...)
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  nick text not null check (char_length(nick) between 2 and 16),
  data jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists profiles_nick_unico on public.profiles (lower(nick));

-- Compras de verdade (Premium, passes, itens). Só o servidor grava aqui, depois que o pagamento é confirmado.
create table if not exists public.entitlements (
  user_id uuid primary key references auth.users(id) on delete cascade,
  premium boolean not null default false,
  passes jsonb not null default '{}'::jsonb,
  owned jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

-- Regras de acesso: cada jogador só lê e edita o próprio perfil; compras ele só lê.
alter table public.profiles enable row level security;
alter table public.entitlements enable row level security;
drop policy if exists "ler o proprio perfil" on public.profiles;
drop policy if exists "editar o proprio perfil" on public.profiles;
drop policy if exists "ler as proprias compras" on public.entitlements;
create policy "ler o proprio perfil" on public.profiles for select using (auth.uid() = id);
create policy "editar o proprio perfil" on public.profiles for update using (auth.uid() = id) with check (auth.uid() = id);
create policy "ler as proprias compras" on public.entitlements for select using (auth.uid() = user_id);

-- Libera a API para essas tabelas (as regras acima continuam valendo).
grant usage on schema public to anon, authenticated;
grant select, update on public.profiles to authenticated;
grant select on public.entitlements to authenticated;

-- Ao criar a conta, já nasce o perfil (com o apelido escolhido) e a linha de compras vazia.
create or replace function public.novo_usuario() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, nick)
  values (new.id, coalesce(nullif(trim(new.raw_user_meta_data->>'nick'), ''), 'jogador' || substr(new.id::text, 1, 6)));
  insert into public.entitlements (user_id) values (new.id);
  return new;
end $$;
drop trigger if exists ao_criar_usuario on auth.users;
create trigger ao_criar_usuario after insert on auth.users for each row execute function public.novo_usuario();

-- ================= PAGAMENTOS (Premium via Cakto) =================
-- Registro de cada pagamento avisado pela Cakto. Sem políticas de acesso: só o servidor (chave secreta) lê e grava.
create table if not exists public.pagamentos (
  order_id text primary key,
  email text not null,
  evento text not null,
  valor numeric,
  user_id uuid references auth.users(id) on delete set null,
  dados jsonb,
  criado_em timestamptz not null default now()
);
alter table public.pagamentos enable row level security;

-- Pagamento aprovado: liga o Premium da conta com esse e-mail (se a conta ainda não existe, liga quando ela for criada).
create or replace function public.liberar_premium(p_email text, p_order text, p_valor numeric, p_dados jsonb) returns text
language plpgsql security definer set search_path = public as $$
declare uid uuid;
begin
  select id into uid from auth.users where lower(email) = lower(trim(p_email)) limit 1;
  insert into public.pagamentos (order_id, email, evento, valor, user_id, dados)
  values (p_order, lower(trim(p_email)), 'aprovado', p_valor, uid, p_dados)
  on conflict (order_id) do update set evento = 'aprovado', user_id = coalesce(excluded.user_id, pagamentos.user_id);
  if uid is null then return 'sem_conta'; end if;
  insert into public.entitlements (user_id, premium) values (uid, true)
  on conflict (user_id) do update set premium = true, updated_at = now();
  return 'ok';
end $$;

-- Reembolso ou chargeback: desliga o Premium.
create or replace function public.remover_premium(p_email text, p_order text, p_evento text) returns text
language plpgsql security definer set search_path = public as $$
declare uid uuid;
begin
  update public.pagamentos set evento = p_evento where order_id = p_order;
  select id into uid from auth.users where lower(email) = lower(trim(p_email)) limit 1;
  if uid is null then return 'sem_conta'; end if;
  update public.entitlements set premium = false, updated_at = now() where user_id = uid;
  return 'ok';
end $$;
revoke all on function public.liberar_premium(text, text, numeric, jsonb) from public, anon, authenticated;
revoke all on function public.remover_premium(text, text, text) from public, anon, authenticated;
grant execute on function public.liberar_premium(text, text, numeric, jsonb) to service_role;
grant execute on function public.remover_premium(text, text, text) to service_role;

-- Ao criar a conta: perfil + compras; se esse e-mail já pagou antes de criar a conta, já nasce Premium.
create or replace function public.novo_usuario() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, nick)
  values (new.id, coalesce(nullif(trim(new.raw_user_meta_data->>'nick'), ''), 'jogador' || substr(new.id::text, 1, 6)));
  insert into public.entitlements (user_id, premium)
  values (new.id, exists (select 1 from public.pagamentos where email = lower(new.email) and evento = 'aprovado'));
  update public.pagamentos set user_id = new.id where email = lower(new.email) and user_id is null;
  return new;
end $$;

-- Confere se um apelido está livre sem expor a lista de jogadores.
create or replace function public.nick_disponivel(n text) returns boolean
language sql security definer set search_path = public as $$
  select not exists (select 1 from public.profiles where lower(nick) = lower(trim(n)));
$$;
grant execute on function public.nick_disponivel(text) to anon, authenticated;
