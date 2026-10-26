select
    lane,
    date_trunc('month', dispatched_on)::date                                   as month,
    count(*)                                                                   as shipments,
    round(100.0 * avg(case when on_time then 1 else 0 end) filter (where on_time is not null), 1) as on_time_pct,
    percentile_cont(0.5) within group (order by transit_hours)                 as p50_transit_hours,
    percentile_cont(0.9) within group (order by transit_hours)                 as p90_transit_hours,
    sum(freight_amount)                                                        as freight_billed,
    -- month-over-month change in on-time rate for the same lane
    round(
        100.0 * avg(case when on_time then 1 else 0 end) filter (where on_time is not null)
        - lag(100.0 * avg(case when on_time then 1 else 0 end) filter (where on_time is not null))
            over (partition by lane order by date_trunc('month', dispatched_on)),
        1
    )                                                                          as on_time_pct_change
from {{ ref('fct_shipments') }}
where status = 'DELIVERED'
group by lane, date_trunc('month', dispatched_on)
