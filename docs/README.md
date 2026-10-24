# PaySentry documentation

PaySentry is a portfolio payment system built around a double-entry ledger and controllable mock gateways.
Start with a payment walkthrough, then follow the design decisions into the code and tests.

| Read this | To understand |
| --- | --- |
| [Building PaySentry](build-story.md) | The project from prototype through ledger, payments, recovery and contextual routing |
| [Local development](development.md) | Docker, uv, PowerShell examples, every application setting and setup problems |
| [Architecture and data model](architecture.md) | Module boundaries, tables, posting rules, transaction boundaries and shared state |
| [API reference](api.md) | Every application route, authentication, request fields, response states and errors |
| [Operations](operations.md) | Health, pending payments, recovery, reconciliation and mock failure demonstrations |
| [Hosting](hosting.md) | The free Render Blueprint for the browser demo, its limits and restart behavior |
| [Testing and extension](testing.md) | Test fixtures, coverage by behavior, focused commands, simulation and CI |
| [Study guide](guide.md) | A deeper teaching walkthrough with questions and exercises |
| [Portfolio roadmap](roadmap.md) | Proposed milestones, completion checks and optional extensions |

The design documents describe individual mechanisms:

- [Payments](design/payments.md): lifecycle, refunds, merchant identity, idempotency and card handling.
- [Gateways](design/gateways.md): contracts, normalized outcomes, stable references and leased recovery.
- [Routing](design/routing.md): eligibility, breakers, Thompson sampling, LinUCB and simulations.
- [Demo](design/demo.md): browser sessions, fixed payment actions, limits and hosting choices.

For the original SQLAlchemy transaction API, read the [prototype notes](../prototype/README.md).
The root app does not use that archived implementation.

The build story follows commits already present in Git history.
It separates working features from the proposed roadmap.
Keep documentation changes beside the feature changes they describe, using actual commit dates.

When changing behavior, update the relevant design document, API reference, and test map.
Verify local links, example commands, request fields, defaults, and response codes against the implementation.
