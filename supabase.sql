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

-- Confere se um apelido está livre sem expor a lista de jogadores.
create or replace function public.nick_disponivel(n text) returns boolean
language sql security definer set search_path = public as $$
  select not exists (select 1 from public.profiles where lower(nick) = lower(trim(n)));
$$;
grant execute on function public.nick_disponivel(text) to anon, authenticated;
