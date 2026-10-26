{% test valid_gstin(model, column_name) %}
select {{ column_name }} as invalid_gstin
from {{ model }}
where {{ column_name }} is not null
  and not {{ gstin_is_valid(column_name) }}
{% endtest %}
