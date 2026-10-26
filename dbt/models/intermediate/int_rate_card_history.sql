-- Rate cards as validity ranges (SCD type 2 style): each rate applies from effective_from until the next revision.
select
    origin,
    destination,
    vehicle_class_t,
    rate_per_trip,
    effective_from as valid_from,
    coalesce(
        lead(effective_from) over (partition by origin, destination, vehicle_class_t order by effective_from),
        date '9999-12-31'
    ) as valid_to
from {{ ref('stg_rates__rate_cards') }}
