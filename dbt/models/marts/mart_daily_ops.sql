-- Time series for the ops dashboard (Grafana). Aggregate once per measure, then join to a date spine:
-- gap days show as zero instead of disappearing from the chart.
with f as (
    select * from {{ ref('fct_shipments') }}
),

spine as (
    select generate_series(min(dispatched_on), max(coalesce(delivered_at::date, dispatched_on)), interval '1 day')::date as day
    from f
),

dispatched as (
    select dispatched_on as day, count(*) as dispatched from f group by 1
),

delivered as (
    select delivered_at::date as day, count(*) as delivered, count(*) filter (where not on_time) as delivered_late
    from f where delivered_at is not null group by 1
),

billed as (
    select invoice_date as day, sum(freight_amount) as freight_billed from f where invoice_date is not null group by 1
)

select
    s.day,
    coalesce(dispatched, 0)     as dispatched,
    coalesce(delivered, 0)      as delivered,
    coalesce(delivered_late, 0) as delivered_late,
    coalesce(freight_billed, 0) as freight_billed
from spine as s
left join dispatched using (day)
left join delivered using (day)
left join billed using (day)
order by s.day
