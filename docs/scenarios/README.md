# Scenarios

Six pull-request shapes run against `examples/webshop`, recorded by `scripts/scenarios.py`. Four must fail for the stated reason; two must pass, one of them with every test green and the numbers moved.

| Scenario | Expected | Result |
| --- | --- | --- |
| [Rename a source column without updating staging](rename-source-column.md) | fail | ✅ as expected |
| [Delete a model that two marts `ref()`](drop-ref.md) | fail | ✅ as expected |
| [Break a foreign key so a relationships test fails](break-join.md) | fail | ✅ as expected |
| [Change discount arithmetic a unit test covers](unit-test-catch.md) | fail | ✅ as expected |
| [Count cancelled orders in lifetime revenue, with no test on it](silent-logic-change.md) | pass | ✅ as expected |
| [Reformat a staging model](harmless-refactor.md) | pass | ✅ as expected |
