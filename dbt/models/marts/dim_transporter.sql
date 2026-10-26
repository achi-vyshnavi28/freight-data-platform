-- GSTIN comes from the ERP (the TMS doesn't carry it); most frequent value wins if a transporter has several.
with gstins as (
    select transporter, seller_gstin, count(*) as n,
           row_number() over (partition by transporter order by count(*) desc) as rnk
    from {{ ref('stg_erp__invoices') }}
    group by transporter, seller_gstin
)

select
    t.transporter,
    g.seller_gstin                       as gstin,
    c.state                              as home_state,
    min(s.booked_at)::date               as first_shipment_on,
    count(s.shipment_id)                 as lifetime_shipments
from (select distinct transporter from {{ ref('stg_tms__shipments') }}) as t
left join gstins as g on g.transporter = t.transporter and g.rnk = 1
left join (select distinct state_code, state from {{ ref('cities') }}) as c on c.state_code::text = left(g.seller_gstin, 2)
left join {{ ref('stg_tms__shipments') }} as s on s.transporter = t.transporter
group by t.transporter, g.seller_gstin, c.state
