# Building PaySentry

I started PaySentry as a fraud detection idea. The first working result was a small transaction API.
I then narrowed the problem: record money correctly when requests repeat, race, or lose their gateway response.
That smaller problem became the foundation for payments, recovery, and routing.

This walkthrough follows the implementation sequence in the repository.
Dates below are the dates recorded on the existing `main` branch.
They identify milestones, rather than estimating time spent learning or implementing each feature.

## January and February: the prototype

The [prototype](../prototype/README.md) contains the first FastAPI application and its SQLAlchemy models.
The transaction endpoint eventually wrote to Postgres.
Commit `c82ae45`, dated February 8, records database persistence.

The original ambition included fraud scoring, linked-account analysis, and a documentation assistant.
Those ideas did not become working features in the prototype.
The surviving code is useful because it makes the starting point concrete.

Two choices needed to change before this could become a payment ledger.
The prototype stored amounts as floating-point numbers and had no request idempotency.
A retry could create another transaction, and decimal arithmetic could introduce rounding errors.

I preserved that version in `prototype/` instead of mixing its models with the rebuild.
Commit `e5619db`, dated February 15, records the move.
The root application does not import the prototype.

## February and March: make one transfer correct

The root rebuild starts with FastAPI, asyncpg, Postgres, and Redis in commit `66c9dbe` on February 20.
I used plain SQL so transaction boundaries and row locks would remain visible.

The first important unit was a transfer between two accounts.
Commit `f45560f`, dated March 3, adds the double-entry ledger.
Each successful transfer subtracts from one account and adds the same amount to another.
Amounts use integer minor units: 50000 means INR 500.00.

One database transaction owns the transfer, its two ledger entries, account balances, and stored response.
If any step fails, the whole transaction rolls back.
The database rejects changes to existing ledger entries.
Corrections therefore need new postings.

Concurrency required more than a transaction around ordinary reads.
Two requests could both observe the same available balance.
I locked account rows before checking funds and always locked them in account ID order.
That gives opposite transfers the same lock order.

I used `FOR NO KEY UPDATE` because transfer inserts already take foreign-key locks on account rows.
A stronger lock would introduce conflicts that the balance update does not need.
See [the ledger implementation](../app/ledger.py) and [concurrency tests](../tests/test_concurrency.py).

Next came request replay.
A unique key lets Postgres decide which concurrent request owns a transfer.
The winner stores the response. Later copies return that response without moving money again.
A fingerprint rejects reuse of the key for a different request.

Redis rate limiting arrived on March 7 in `fa3c2ad`.
Reconciliation followed on March 17 in `681e7df`.
The real-database concurrency tests arrived on March 24 in `9b5134b`.
Commit `78bd9ff`, dated March 30, records exact response replay, documentation, and CI.

The important lesson was to test invariants under competing requests.
A successful HTTP response alone does not prove that balances remain correct.

## April and May: turn transfers into merchant payments

The ledger rebuild merges in `26d1d99`, dated April 9.
Merchant identity arrives in `0c2df46` on April 24.
An administrator creates a merchant, which receives one secret API key and one account per supported currency.
The database stores the key hash and a short display prefix.

Merchant operations needed their own idempotency scope.
Two merchants should be able to use `order-1` independently.
Commit `b096bac`, dated April 30, adds the merchant key store and a composable ledger posting function.
That function lets payment state and money movement commit inside the same transaction.

Card validation appears on May 8 in `9820a59`.
Only card brand and last four digits belong in payment rows.
Validation errors also need care because an ordinary framework error can echo rejected card input.
The [error handler](../app/errors.py) removes that input from validation responses.

Payment creation follows on May 17 in `0dce315`.
Automatic capture posts money immediately after gateway approval.
Manual capture creates an authorization without a ledger posting.
Captures and voids follow in `8abfe48`, dated May 27.

A partial capture consumes the authorization once.
The remaining authorized amount cannot be captured later in this implementation.
This keeps the capture posting keyed by payment ID.

Refunds arrive in `b1b3e6b` on May 31.
Pending refunds reserve their amounts while the gateway call runs.
Otherwise two concurrent refunds could each spend the same refundable balance.
The available amount subtracts both completed and pending refunds.

## June: check the payment model against the ledger

Commit `5f70813`, dated June 8, extends reconciliation and adds payment concurrency and model-based tests.
The tests compare random operation sequences with a small independent payment model.

The resulting questions are concrete.
Can capture and void both win? Can concurrent refunds exceed the capture?
Does every captured amount have its matching ledger posting?
Does each failed operation leave the appropriate amount available?

The [payment design](design/payments.md) documents the state machine and lock order.
Commit `843cddd`, dated June 18, records that documentation milestone.
Gateway and recovery design begins in `ab683cb` on June 27.

## July: handle uncertainty outside the database

A database transaction cannot include the remote gateway's decision.
Holding row locks during a slow gateway call would also stall unrelated requests.
I split an operation into three parts:

1. Commit the request key and pending operation.
2. Call the gateway without an open database transaction.
3. Commit the result, ledger postings, and final response together.

The gap between steps two and three is unavoidable.
The process might stop after the gateway charged the card.
The next request must distinguish an unknown outcome from a definite refusal to process.

Mock gateways arrive in `d87f37a`, dated July 3.
Connectors arrive in `3274b97` on July 10.
They normalize two different HTTP contracts into approved, declined, not processed, and unknown outcomes.
Lookup can additionally report that an authorization was not found.

Payments use those connectors from `d832d4c`, dated July 20.
Recovery follows in `6b87d1c` on July 23.
An unknown authorization is looked up using its original attempt reference.
Capture, void, and refund recovery reuse stable operation references.

Workers take short leases instead of holding transactions across network calls.
The request path and recovery share finalization functions.
Conditional state changes let one finisher win without duplicating a posting.
The [gateway design](design/gateways.md) explains the assumptions this depends on.

## August: choose gateways and fail over safely

Gateway documentation and Compose integration arrive in `e9bbb3d` on August 4.
Routing design follows on August 9 in `d734c59`.

Commit `b3b5491`, dated August 16, adds capability rules, circuit breakers, and success-rate ranking.
Capability rules first remove gateways that cannot accept the currency or card brand.
Circuit breakers avoid gateways with repeated technical failures.
A genuine card decline counts against approval rate but still proves the gateway answered.

Commit `812db6f`, dated August 24, connects routing and failover to payment creation.
The safety rule is narrow: try another gateway only when the previous one definitely did not process.
An unknown outcome stops failover because another charge could duplicate the first one.
Capture, void, and refund remain attached to the gateway holding the authorization.

## September and October: measure routing by payment context

Commit `a5ac5ff`, dated September 1, adds outage simulation.
Commit `754d208`, dated September 11, makes completed request replay independent of gateway availability.
Gateway health should never prevent a merchant from reading its stored result.

Aggregate approval rates hide differences between payment segments.
A gateway might approve Visa reliably while declining Mastercard often.
The contextual design appears in `974ba2e` on September 16.
Segment-dependent mock declines follow in `da5a578` on September 26.

Commit `2b36641`, dated October 4, adds LinUCB routing.
The model uses currency, card brand, amount buckets, and brand-by-large-amount features.
Each gateway has separate model statistics in Redis.
An uncertainty bonus allows exploration, while decay reduces the weight of old observations.

Commit `d9d2404`, dated October 11, connects contextual routing and segment simulation.
The [routing design](design/routing.md) contains the equations and simulation method.
The [simulation script](../scripts/simulate_routing.py) compares strategies under outages and segment shifts.

These experiments demonstrate routing behavior against controlled mock gateways.
They do not establish live provider performance or fraud detection accuracy.

## What the build establishes

The useful unit is a payment whose request, gateway operation, local state, and ledger entries remain explainable.
The tests cover repeated requests, competing operations, and recovery after uncertain gateway outcomes.
Each layer adds a specific responsibility instead of replacing the ledger's invariants.

The next portfolio work is described in the [roadmap](roadmap.md).
Migrations, durable merchant events, visibility, load measurements, and a dashboard make this behavior easier to demonstrate.
Settlement, payouts, fraud scoring, and real provider integration remain separate future projects.

To inspect a milestone locally, run `git show <commit>` or `git log --reverse --date=short`.
New changes should use their actual commit dates and describe the work they contain.
