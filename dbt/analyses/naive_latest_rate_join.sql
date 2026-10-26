-- The tempting but wrong way: compare every invoice with the CURRENT rate card.
-- Rates rose 6% on 1 July 2026. Against the higher July rate, a June invoice padded by 5-8% looks
-- normal, so this query silently MISSES June overbilling. fct_shipments joins the rate in force on the
-- dispatch date instead. Run both counts to see the gap.
with latest_rate as (
    select distinct on (origin, destination, vehicle_class_t) origin, destination, vehicle_class_t, rate_per_trip
    from {{ ref('stg_rates__rate_cards') }}
    order by origin, destination, vehicle_class_t, effective_from desc
)
select
    count(*) filter (where s.freight_amount > r.rate_per_trip * 1.02) as flagged_with_latest_rate,
    count(*) filter (where s.is_overbilled)                          as flagged_with_effective_rate
from {{ ref('fct_shipments') }} as s
join latest_rate as r using (origin, destination, vehicle_class_t)
