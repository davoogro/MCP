from mssql_mcp.db import fit_response
from mssql_mcp.server import summarize_plan

PLAN = """<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan"><BatchSequence><Batch>
<Statements><StmtSimple StatementText="SELECT * FROM t" StatementSubTreeCost="1.5" StatementEstRows="100">
<QueryPlan><MissingIndexes><MissingIndexGroup Impact="90.5"><MissingIndex Database="[db]" Schema="[dbo]" Table="[t]">
<ColumnGroup Usage="EQUALITY"><Column Name="[a]"/></ColumnGroup></MissingIndex></MissingIndexGroup></MissingIndexes>
<RelOp NodeId="0" PhysicalOp="Clustered Index Scan" LogicalOp="Clustered Index Scan" EstimateRows="100"
 EstimatedTotalSubtreeCost="1.5"><IndexScan><Object Database="[db]" Schema="[dbo]" Table="[t]" Index="[pk]"/></IndexScan>
</RelOp></QueryPlan></StmtSimple></Statements></Batch></BatchSequence></ShowPlanXML>"""


def test_summarize_plan():
    s = summarize_plan(PLAN)
    assert s["statements"][0]["estimated_cost"] == 1.5
    assert s["top_operators"][0]["object"] == "[db].[dbo].[t].[pk]"
    assert s["missing_indexes"][0]["equality"] == ["[a]"]


def test_fit_response():
    p = {"ok": True, "rows": [["x" * 100]] * 1000, "row_count": 1000}
    out = fit_response(p, 10_000)
    assert out["response_truncated"] and len(out["rows"]) < 1000
    small = {"ok": True, "rows": [[1]]}
    assert "response_truncated" not in fit_response(small, 10_000)
