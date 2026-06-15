This is a design session. The output is a design spec, not code.

As part of the design, include:

**Tests**
List the test cases that would validate this design. For each: what is being tested, what the expected behaviour is, and what edge cases or failure modes are covered.

**Logging**
Review the proposed changes against the existing OrchestratorEvent schema. Only propose additions — a new event type or additional fields on an existing event — where the absence of that data would make a real failure harder to diagnose. For any proposed addition, state what failure scenario it helps with and what data it captures.