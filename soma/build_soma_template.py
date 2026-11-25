"""Generate the Soma SQLite database template with schema, views,
placeholder data, and all 18 stage instructions loaded from machine-talk/*.md."""

import re
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone

# ── Paths ───────────────────────────────────────────────────────────────
# Resolved from this file, so the build never reads or writes outside soma-nov.
BASE = Path(__file__).resolve().parent
MACHINETALK = BASE / "machine-talk"
OUTPUT = BASE / "soma_template.sql"

# ── Stage instruction loading ──────────────────────────────────────────
STAGE_CONFIG = [
    ("tb-001-distiller.v3.0.0.md",  "distiller",   "tb-001", None,    True),
    ("tb-002-pipeline-protocol.v2.md","pipeline_protocol","tb-002", None, True),
    ("tb-003-system-base.v2.md",    "system_base", "tb-003", None, True),
    ("tb-004-introbeaver.v2.md",    "introbeaver", "tb-004", None, True),
    ("tb-005-planbeaver.v2.md",     "planbeaver",  "tb-005", None, True),
    ("tb-006-outrobeaver.v2.md",    "outrobeaver", "tb-006", None, True),
    ("tb-007-nexus.v2.md",          "nexus",       "tb-007", None, True),
    ("tb-008-spark.v2.md",          "spark",       "tb-008", None, True),
    ("tb-009-falcon.v2.md",         "falcon",      "tb-009", None, True),
    ("tb-010-eagle.v2.md",          "eagle",       "tb-010", None, True),
    ("tb-011-drafter.v3.0.0.md",    "drafter",     "tb-011", None, True),
    ("tb-015-axonhillock.v1.md",    "axonhillock", "tb-015", None, True),
    ("tb-016-terminalplayer.v1.md", "terminalplayer", "tb-016", None, True),
    ("tb-017-saltatorygate.v1.md",  "saltatorygate", "tb-017", None, True),
    ("tb-018-codepublisher.v1.md",  "codepublisher", "tb-018", None, True),
    ("tb-012-scout.v2.md",          "scout",       "tb-012", None, True),
    ("tb-013-oc.v2.md",             "oc",          "tb-013", None, True),
    ("tb-014-olc.v2.md",            "olc",         "tb-014", None, True),
]

def load_stage_instructions():
    """Read each .md file and return (stage_name, tb_number, body) tuples."""
    instructions = []
    for filename, stage_name, tb_number, _, handoff in STAGE_CONFIG:
        fpath = MACHINETALK / filename
        if not fpath.exists():
            raise FileNotFoundError(f"Missing: {fpath}")
        body = fpath.read_text(encoding="utf-8")
        # Strip leading markdown headers (version/classification lines)
        body = re.sub(r'^#\s+#?\s*VERSION:\s+.*?\n', '', body, flags=re.M)
        body = re.sub(r'^#\s+#?\s*CLASSIFICATION:\s+.*?\n', '', body, flags=re.M)
        body = re.sub(r'^#\s+#?\s*SUPERSEDES:\s+.*?\n', '', body, flags=re.M)
        body = re.sub(r'^#\s+#?\s*TOKEN_CEILING:\s+.*?\n', '', body, flags=re.M)
        instructions.append((stage_name, tb_number, body.strip()))
    return instructions

# ── SQL Schema ──────────────────────────────────────────────────────────
SCHEMA = """\
-- ═══════════════════════════════════════════════════════════════════
-- SOMA SQLITE DATABASE TEMPLATE
-- Per-session graph store with stage instructions and controlled projections
-- Generated: {generated_at}
-- ═══════════════════════════════════════════════════════════════════

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA encoding='UTF-8';
PRAGMA page_size=4096;

-- ── Sessions table ──────────────────────────────────────────────────
CREATE TABLE sessions (
    session_id        TEXT PRIMARY KEY,
    project_context   TEXT DEFAULT 'Dendritic',
    total_sets        INTEGER DEFAULT 0,
    total_session_chars INTEGER DEFAULT 0,
    created_at        TEXT DEFAULT (datetime('now'))
);

-- ── Nodes table ─────────────────────────────────────────────────────
CREATE TABLE nodes (
    node_id     TEXT PRIMARY KEY,
    node_type   TEXT NOT NULL CHECK(node_type IN (
        'SessionAnchor', 'ChatSet', 'Message', 'ReceptorLog',
        'DistilledChatSet', 'Directive', 'Constraint', 'Artifact',
        'Stack', 'Chronology', 'NoiseSignature', 'Entity',
        'Chunk', 'ChatSetGroup', 'IntentEntry', 'IntentNode', 'FunctionSlot'
    )),
    properties  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_nodes_type   ON nodes(node_type);
CREATE INDEX idx_nodes_sid    ON nodes(properties->>'$.session_id');
CREATE INDEX idx_nodes_csid   ON nodes(properties->>'$.chat_set_id');

-- ── Edges table ─────────────────────────────────────────────────────
CREATE TABLE edges (
    edge_id   TEXT PRIMARY KEY,
    source    TEXT NOT NULL REFERENCES nodes(node_id),
    rel_type  TEXT NOT NULL CHECK(rel_type IN (
        'CONTAINS', 'HAS_MESSAGE', 'PROCESSED_BY', 'DISTILLED_TO',
        'HAS_DIRECTIVE', 'HAS_CONSTRAINT', 'HAS_ARTIFACT',
        'HAS_STACK', 'HAS_CHRONOLOGY', 'HAS_NOISE',
        'HAS_CHATSET_GROUP', 'HAS_CHUNK', 'NEXT_CHUNK', 'NEXT_GROUP',
        'YIELDS', 'GOVERNS', 'LINKED_TO', 'SLICED_FROM', 'TRACES_TO'
    )),
    target    TEXT NOT NULL REFERENCES nodes(node_id)
);
CREATE INDEX idx_edges_source ON edges(source);
CREATE INDEX idx_edges_target ON edges(target);
CREATE INDEX idx_edges_rel    ON edges(rel_type);

-- ── Stage Instructions table ────────────────────────────────────────
CREATE TABLE stage_instructions (
    stage_name  TEXT PRIMARY KEY,
    tb_number   TEXT,
    body        TEXT NOT NULL,
    handoff     INTEGER DEFAULT 0,
    loaded_from TEXT,
    version     INTEGER DEFAULT 1
);
"""

# ── Views ─────────────────────────────────────────────────────────────
VIEWS = """\
-- ═══════════════════════════════════════════════════════════════════
-- CONTROLLED PROJECTIONS (per-stage views)
-- Each stage queries its view, never the base tables directly.
-- ═══════════════════════════════════════════════════════════════════

-- Stage 1: Distiller — raw chatsets only
CREATE VIEW stage1_distiller_input AS
  SELECT n.node_id, n.properties
  FROM nodes n
  WHERE n.node_type = 'ChatSet';

-- Stage 1: IntroBeaver — raw chatsets (all messages)
CREATE VIEW stage1_introbeaver_input AS
  SELECT n.node_id, n.properties,
         e.source AS chatset_ref
  FROM nodes n
  JOIN edges e ON n.node_id = e.target
  WHERE e.rel_type = 'HAS_MESSAGE';

-- Stage 1: IntroBeaver — distilled output from distiller
CREATE VIEW stage1_distiller_output AS
  SELECT n.node_id, n.properties
  FROM nodes n
  WHERE n.node_type = 'DistilledChatSet';

-- Stage 1: PlanBeaver — distilled + directives + constraints
CREATE VIEW stage1_planbeaver_input AS
  SELECT n.node_id, n.properties, e.rel_type
  FROM nodes n
  JOIN edges e ON n.node_id = e.target
  WHERE n.node_type IN ('DistilledChatSet', 'Directive', 'Constraint');

-- Stage 1: OutroBeaver — all stage 1 outputs for session
CREATE VIEW stage1_outrobeaver_input AS
  SELECT n.node_id, n.properties, n.node_type
  FROM nodes n
  WHERE n.node_type IN ('DistilledChatSet', 'Directive', 'Constraint', 'Artifact', 'Stack', 'Chronology', 'NoiseSignature');

-- Stage 2: Nexus — all distilled chatsets for session
CREATE VIEW stage2_nexus_input AS
  SELECT n.node_id, n.properties
  FROM nodes n
  WHERE n.node_type = 'DistilledChatSet'
  ORDER BY CAST(json_extract(n.properties, '$.set_number') AS INTEGER);

-- Stage 2: Spark — blueprint from nexus
CREATE VIEW stage2_spark_input AS
  SELECT n.node_id, n.properties
  FROM nodes n
  WHERE n.node_type = 'StrategicBlueprintNode'
     OR n.node_type = 'SessionAnchor';

-- Stage 2: Falcon — spark requirements
CREATE VIEW stage2_falcon_input AS
  SELECT n.node_id, n.properties
  FROM nodes n
  WHERE n.node_type = 'RequirementsNode'
     OR n.node_type = 'StrategicBlueprintNode';

-- Stage 3: Eagle — falcon execution plan
CREATE VIEW stage3_eagle_input AS
  SELECT n.node_id, n.properties
  FROM nodes n
  WHERE n.node_type = 'ArchitectureNode'
     OR n.node_type = 'ArchitectureContractNode'
     OR n.node_type = 'RequirementsNode';

-- Stage 3: Drafter — eagle architecture
CREATE VIEW stage3_drafter_input AS
  SELECT n.node_id, n.properties
  FROM nodes n
  WHERE n.node_type IN ('ArchitectureNode', 'FileNode', 'ReferenceArtifactNode')
     OR n.node_type = 'ArchitectureContractNode';

-- Stage 3: Scout — draft artifacts
CREATE VIEW stage3_scout_input AS
  SELECT n.node_id, n.properties
  FROM nodes n
  WHERE n.node_type IN ('FileNode', 'ReferenceArtifactNode', 'ArchitectureContractNode');

-- Stage 4: OC (AxonHillock) — scout report
CREATE VIEW stage4_oc_input AS
  SELECT n.node_id, n.properties
  FROM nodes n
  WHERE n.node_type IN ('InvariantQueryNode', 'FunctionSlot');

-- Stage 4: OLC (Myelinator) — approved drafts
CREATE VIEW stage4_olc_input AS
  SELECT n.node_id, n.properties
  FROM nodes n
  WHERE n.node_type IN ('ImplementationNode', 'SmokeTestNode', 'OptimizedSlotNode', 'SessionManifestNode');

-- Universal read views (for debugging / introspection)
CREATE VIEW node_types AS
  SELECT node_type, COUNT(*) AS count
  FROM nodes
  GROUP BY node_type;

CREATE VIEW edge_types AS
  SELECT rel_type, COUNT(*) AS count
  FROM edges
  GROUP BY rel_type;

-- Session summary view
CREATE VIEW session_summary AS
  SELECT
    s.session_id,
    s.project_context,
    s.total_sets,
    s.total_session_chars,
    COUNT(DISTINCT CASE WHEN n.node_type = 'ChatSet' THEN n.node_id END) AS chatset_count,
    COUNT(DISTINCT CASE WHEN n.node_type = 'DistilledChatSet' THEN n.node_id END) AS distilled_count,
    COUNT(DISTINCT CASE WHEN e.rel_type = 'HAS_DIRECTIVE' THEN e.edge_id END) AS directive_count,
    COUNT(DISTINCT CASE WHEN e.rel_type = 'HAS_CONSTRAINT' THEN e.edge_id END) AS constraint_count,
    COUNT(DISTINCT CASE WHEN e.rel_type = 'HAS_ARTIFACT' THEN e.edge_id END) AS artifact_count
  FROM sessions s
  LEFT JOIN nodes n ON s.session_id = json_extract(n.properties, '$.session_id')
  LEFT JOIN edges e ON n.node_id = e.source
  GROUP BY s.session_id;
"""

# ── Placeholder Data ──────────────────────────────────────────────────
def sql_json(obj):
    """Convert Python dict to SQL-safe compact JSON string."""
    raw = json.dumps(obj, separators=(",", ":"))
    return raw.replace("'", "''")


def main():
    print("Loading stage instructions...")
    instructions = load_stage_instructions()
    total_bytes = sum(len(b) for _, _, b in instructions)
    print(f"  {len(instructions)} instructions loaded, {total_bytes:,} bytes total")

    # Escape SQL strings
    def sql_str(s):
        return s.replace("'", "''")

    # Build SQL
    sql_parts = []
    sql_parts.append(SCHEMA.format(generated_at=datetime.now(timezone.utc).isoformat()))

    # Insert stage instructions
    sql_parts.append("-- ═══════════════════════════════════════════════════════════════════\n-- STAGE INSTRUCTIONS (loaded from machine-talk/*.v2.md)\n-- ═══════════════════════════════════════════════════════════════════\n")
    source_files = {cfg[1]: cfg[0] for cfg in STAGE_CONFIG}
    for stage_name, tb_number, body in instructions:
        tb_num = f"'{tb_number}'"
        handoff_val = 1 if tb_number in ("tb-002",) else 0
        loaded_from = f"'machine-talk/{source_files[stage_name]}'"
        sql_parts.append(
            f"INSERT INTO stage_instructions (stage_name, tb_number, body, handoff, loaded_from, version) "
            f"VALUES ('{stage_name}', {tb_num}, '{sql_str(body)}', {handoff_val}, {loaded_from}, 1);\n"
        )

    sql_parts.append("\n" + VIEWS)

    # No data rows. A new Soma holds the schema, the views and the stage
    # instructions and nothing else: the seeder adds the SessionAnchor and the
    # ChatSets, and the stages add everything after that.

    sql_parts.append("\n-- ═══════════════════════════════════════════════════════════════════\n"
                     "-- END OF TEMPLATE\n"
                     "-- To initialize: sqlite3 somas/<session_id>/soma.db < soma_template.sql\n"
                     "-- ═══════════════════════════════════════════════════════════════════\n")

    # Write
    sql = "\n".join(sql_parts)
    OUTPUT.write_text(sql, encoding="utf-8")
    print(f"\nWritten: {OUTPUT}")
    print(f"Size: {OUTPUT.stat().st_size:,} bytes")
    print(f"Statements: {sql.count(';')}")

if __name__ == "__main__":
    main()
