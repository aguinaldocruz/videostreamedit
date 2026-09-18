# Python module and route inventory

Generated 2026-09-18. Evidence only; no modules were removed.

## Direct app imports from `app/v2.py`

- `app.pg_compat.connect`

## Route decorator counts by module

| Module | Route decorators |
|---|---:|
| `v11.py` | 41 |
| `v13.py` | 21 |
| `v16.py` | 1 |
| `v19.py` | 162 |
| `v2.py` | 20 |
| `v22.py` | 17 |
| `v25.py` | 2 |
| `v28.py` | 14 |
| `v3.py` | 1 |
| `v30.py` | 2 |
| `v31.py` | 2 |
| `v32.py` | 4 |
| `v34.py` | 2 |
| `v37.py` | 1 |
| `v38.py` | 10 |
| `v39.py` | 5 |
| `v40.py` | 7 |
| `v43.py` | 17 |
| `v44.py` | 2 |
| `v48.py` | 5 |
| `v49.py` | 6 |
| `v5.py` | 16 |
| `v50.py` | 9 |
| `v51.py` | 17 |
| `v52.py` | 2 |
| `v53.py` | 1 |
| `v54.py` | 9 |
| `v55.py` | 2 |
| `v57.py` | 1 |
| `v59.py` | 17 |
| `v6.py` | 3 |
| `v63.py` | 11 |
| `v64.py` | 4 |
| `v65.py` | 114 |
| `v67.py` | 3 |
| `v68.py` | 74 |
| `v69.py` | 3 |
| `v7.py` | 15 |
| `v71.py` | 1 |
| `v74.py` | 12 |
| `v77.py` | 9 |
| `v78.py` | 1 |
| `v79.py` | 86 |
| `v8.py` | 5 |
| `v80.py` | 32 |
| `v81.py` | 4 |
| `v82.py` | 53 |
| `v83.py` | 8 |
| `v84.py` | 5 |
| `v86.py` | 18 |

## Cleanup rule

- A module is not removable merely because it is not directly imported by `v2.py`; middleware, dynamic imports, compatibility endpoints, and startup hooks must be checked.
- Any removal requires compile, Compose, smoke, recovery, approval, and performance gates.
