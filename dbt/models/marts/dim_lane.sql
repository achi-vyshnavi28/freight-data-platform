select
    o.city || ' -> ' || d.city                                                       as lane,
    o.city                                                                          as origin,
    d.city                                                                          as destination,
    round(({{ haversine_km('o.lat', 'o.lon', 'd.lat', 'd.lon') }} * 1.25)::numeric, 1) as road_km_estimate,
    o.state_code = d.state_code                                                     as is_intra_state
from {{ ref('cities') }} as o
cross join {{ ref('cities') }} as d
where o.city <> d.city
  and exists (
      select 1 from {{ ref('stg_tms__shipments') }} as s
      where s.origin = o.city and s.destination = d.city
  )
