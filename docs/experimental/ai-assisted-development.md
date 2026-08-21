# 🤖 AI-assisted development

The experimental Durable Workers contribution in this branch was developed
with substantial AI assistance, including architecture exploration, code
authoring, test design and documentation drafting with OpenAI ChatGPT.

The contribution remains **human-directed and human-reviewed**:

- project goals, scope and acceptance criteria are set by the maintainer;
- security boundaries and irreversible actions require human authorization;
- generated changes are reviewed before they are retained;
- H1 through H5 were qualified against real Hermes runtimes, not accepted from
  generated tests alone;
- H6 keeps the same requirement: AI-authored code is not considered validated
  until repository tests and real-runtime qualification pass.

This disclosure applies to the Durable Workers work introduced by MITC on the
experimental branches. It does **not** make any claim about the authorship of
unrelated upstream Hermes Agent code.

The robot marker is intentionally kept in documentation rather than inserted
throughout source files, so the implementation remains conventional and easy
to review.
