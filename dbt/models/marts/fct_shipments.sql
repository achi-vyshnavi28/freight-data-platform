{{ config(indexes=[{'columns': ['shipment_id'], 'unique': true}, {'columns': ['dispatched_on']}, {'columns': ['transporter']}]) }}

-- Grain: one row per valid shipment. The table every dashboard and analysis starts from.
with shipments as (
    select * from {{ ref('stg_tms__shipments') }} where quality_issue is null
),

invoices as (
    select * from {{ ref('stg_erp__invoices') }}
),

joined as (
    select
        s.shipment_id,
        s.transporter,
        s.origin || ' -> ' || s.destination                         as lane,
        s.origin,
        s.destination,
        s.vehicle_no,
        s.vehicle_class_t,
        s.weight_kg,
        s.status,
        s.booked_at,
        s.dispatched_at,
        s.dispatched_at::date                                       as dispatched_on,
        s.promised_at,
        s.delivered_at,
        round((extract(epoch from s.delivered_at - s.dispatched_at) / 3600)::numeric, 2) as transit_hours,
        case when s.delivered_at is not null then s.delivered_at <= s.promised_at end      as on_time,
        greatest(round((extract(epoch from s.delivered_at - s.promised_at) / 3600)::numeric, 2), 0) as delay_hours,
        t.gps_distance_km,
        t.stop_minutes,
        t.long_stops,
        t.ping_count,
        i.invoice_id,
        i.invoice_date,
        i.freight_amount,
        i.other_charges,
        i.total_amount,
        i.arithmetic_ok                                             as invoice_arithmetic_ok,
        i.tax_type_ok                                               as invoice_tax_type_ok,
        -- the rate in force on the day the truck left, not today's rate
        r.rate_per_trip                                             as contracted_rate
    from shipments as s
    left join {{ ref('int_trip_metrics') }} as t using (shipment_id)
    left join invoices as i using (shipment_id)
    left join {{ ref('int_rate_card_history') }} as r
      on r.origin = s.origin
     and r.destination = s.destination
     and r.vehicle_class_t = s.vehicle_class_t
     and s.dispatched_at::date >= r.valid_from
     and s.dispatched_at::date < r.valid_to
)

select
    *,
    invoice_id is not null                                                       as is_invoiced,
    round(freight_amount - contracted_rate, 2)                                   as freight_variance,
    round(100.0 * (freight_amount - contracted_rate) / nullif(contracted_rate, 0), 2) as freight_variance_pct,
    -- 2% tolerance for rounding and toll pass-throughs
    coalesce(freight_amount > contracted_rate * {{ var('overbill_tolerance', 1.02) }}, false) as is_overbilled
from joined
