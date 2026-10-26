with aliases as (select alias, city from {{ ref('city_aliases') }})

select
    coalesce(o.city, initcap(trim(r.origin)))      as origin,
    coalesce(d.city, initcap(trim(r.destination))) as destination,
    r.capacity_t::int                              as vehicle_class_t,
    r.rate_per_trip::numeric(12, 2)                as rate_per_trip,
    r.effective_from::date                         as effective_from
from {{ source('raw', 'rate_cards') }} as r
left join aliases as o on lower(trim(r.origin)) = o.alias
left join aliases as d on lower(trim(r.destination)) = d.alias
