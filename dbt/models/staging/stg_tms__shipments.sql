-- One row per shipment: the latest status snapshot, with every field parsed and normalised.
with source as (
    select * from {{ source('raw', 'tms_shipments') }}
),

aliases as (
    select alias, city from {{ ref('city_aliases') }}
),

parsed as (
    select
        trim(s.shipment_id)                                         as shipment_id,
        trim(s.transporter)                                         as transporter,
        coalesce(o.city, nullif(initcap(trim(s.origin)), ''))      as origin,
        coalesce(d.city, nullif(initcap(trim(s.destination)), '')) as destination,
        o.city is not null and d.city is not null                   as cities_recognised,
        {{ clean_plate('s.vehicle_no') }}                           as vehicle_no,
        {{ parse_weight_kg('s.weight') }}                           as weight_kg,
        upper(trim(s.status))                                       as status,
        {{ parse_ts('s.booked_at') }}                               as booked_at,
        {{ parse_ts('s.dispatched_at') }}                           as dispatched_at,
        {{ parse_ts('s.promised_at') }}                             as promised_at,
        {{ parse_ts('s.delivered_at') }}                            as delivered_at,
        {{ parse_ts('s.updated_at') }}                              as updated_at,
        s._batch_id,
        s._loaded_at
    from source as s
    left join aliases as o on lower(trim(s.origin)) = o.alias
    left join aliases as d on lower(trim(s.destination)) = d.alias
),

latest as (
    select
        *,
        row_number() over (
            partition by shipment_id
            -- latest event wins; on a timestamp tie the later lifecycle stage wins
            order by updated_at desc nulls last,
                     case status when 'DELIVERED' then 3 when 'IN_TRANSIT' then 2 else 1 end desc,
                     _batch_id desc
        ) as version_rank,
        count(*) over (partition by shipment_id) as versions_seen
    from parsed
)

select
    shipment_id,
    transporter,
    origin,
    destination,
    vehicle_no,
    weight_kg,
    -- Contract rates are per vehicle class; a load over 10 t needs the 20 t truck.
    case when weight_kg <= 10000 then 10 else 20 end as vehicle_class_t,
    status,
    booked_at,
    dispatched_at,
    promised_at,
    delivered_at,
    updated_at,
    versions_seen,
    case
        when origin is null or destination is null then 'missing city'
        when not cities_recognised then 'unrecognised city'
        when booked_at is null or promised_at is null then 'unparseable timestamp'
        when weight_kg is null or weight_kg <= 0 or weight_kg > 49000 then 'invalid weight'
        when delivered_at < dispatched_at then 'delivered before dispatch'
    end as quality_issue
from latest
where version_rank = 1
