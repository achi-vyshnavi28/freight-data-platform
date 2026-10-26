-- Dispute worklist for accounts payable: one row per overbilled invoice, biggest first.
select
    invoice_id,
    shipment_id,
    transporter,
    lane,
    vehicle_class_t,
    dispatched_on,
    contracted_rate,
    freight_amount,
    freight_variance,
    freight_variance_pct,
    sum(freight_variance) over (partition by transporter)                       as transporter_total_variance,
    rank() over (order by freight_variance desc)                                as dispute_priority
from {{ ref('fct_shipments') }}
where is_overbilled
