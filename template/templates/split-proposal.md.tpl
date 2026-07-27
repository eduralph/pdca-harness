<!-- pdca:split-proposal v1 -->
# Split proposal — issue <id>

<!-- Delimiters are HTML comments, not headings, DELIBERATELY: each child body is a full
     draft brief and may contain arbitrary headings and fenced code blocks, so anything
     that could also appear inside a child cannot be its boundary. `pdca split --accept`
     parses these markers; keep them exactly as written. -->

## Why this slice is oversized

<the seams you found, in prose — what makes this more than one shippable outcome>

## Wave sketch

<which children are independent (same wave -> run in parallel across lanes) and which must
 stack, WITH THE REASON. This is what makes the scheduler work: `compute_waves` already
 partitions a batch into dependency waves and folds each wave onto an integration branch
 before the next builds, so correct ordering fields here mean ZERO new scheduling code.>

<!-- pdca:child child-1 -->
- **Slug:** <kebab-case>
- **Defect / goal:** <what is broken / what should exist>
- **Success criterion:** <the observable condition that means it is fixed>
- **Scope (one logical fix) / out of scope:** <what this child touches, and what it does not>
- **Test file:** <path the regression ships at>
- **Difficulty:** <low | medium | high>
- **Depends on:** <child-N[, child-N…] — omit if independent>
- **Conflicts with:** <child-N[, child-N…] — omit if none>
<!-- pdca:end child-1 -->

<!-- pdca:child child-2 -->
- **Slug:** <kebab-case>
- **Defect / goal:** <…>
- **Success criterion:** <…>
- **Scope (one logical fix) / out of scope:** <…>
- **Test file:** <…>
- **Difficulty:** <low | medium | high>
- **Depends on:** <…>
- **Conflicts with:** <…>
<!-- pdca:end child-2 -->
