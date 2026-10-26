-- Least-privilege roles. Run once as a superuser after the first dbt build.

-- Dashboards: read the marts, nothing else.
do $$ begin
    if not exists (select from pg_roles where rolname = 'grafana_ro') then
        create role grafana_ro login password 'grafana_ro';
    end if;
end $$;
grant usage on schema marts, quality to grafana_ro;
grant select on all tables in schema marts, quality to grafana_ro;
alter default privileges in schema marts grant select on tables to grafana_ro;

-- AI text-to-SQL: marts only, read-only by default, and a hard timeout at the role level.
do $$ begin
    if not exists (select from pg_roles where rolname = 'ai_analyst') then
        create role ai_analyst login password 'change-me';
    end if;
end $$;
grant usage on schema marts to ai_analyst;
grant select on all tables in schema marts to ai_analyst;
alter default privileges in schema marts grant select on tables to ai_analyst;
alter role ai_analyst set default_transaction_read_only = on;
alter role ai_analyst set statement_timeout = '5s';
