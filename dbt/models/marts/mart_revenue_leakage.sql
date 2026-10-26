-- Money that slips through the cracks between the TMS and the ERP.
-- amount = contracted freight we expected to be billed for (revenue at risk)
select 'delivered, never invoiced' as issue, shipment_id, null::text as invoice_id, transporter, delivered_at::date as event_date, contracted_rate as amount
from {{ ref('fct_shipments') }}
where status = 'DELIVERED' and not is_invoiced

union all

select 'invoice for unknown shipment', i.shipment_id, i.invoice_id, i.transporter, i.invoice_date, i.total_amount
from {{ ref('stg_erp__invoices') }} as i
where not exists (select 1 from {{ ref('stg_tms__shipments') }} as s where s.shipment_id = i.shipment_id)
