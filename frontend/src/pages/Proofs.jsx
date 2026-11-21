import React, { useEffect, useState } from "react";
import { api } from "../api.js";

export default function Proofs() {
  const [rows, setRows] = useState([]);
  const [recs, setRecs] = useState([]);
  const [batchId, setBatchId] = useState("");
  const [verify, setVerify] = useState(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const reload = () =>
    Promise.all([api("/api/proofs"), api("/api/reconcile")])
      .then(([p, r]) => { setRows(p); setRecs(r); })
      .catch((e) => setErr(String(e.message || e)));

  useEffect(() => { reload(); }, []);

  const rebuild = async () => {
    setErr("");
    try {
      const r = await api("/api/proofs/rebuild", { method: "POST", body: "{}" });
      setMsg(JSON.stringify(r, null, 2));
      await reload();
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  const check = async () => {
    setErr("");
    setVerify(null);
    try {
      const v = await api(`/api/reconcile/${encodeURIComponent(batchId.trim())}`);
      setVerify(v);
    } catch (e) {
      setErr(String(e.message || e));
    }
  };

  const validBadge = (v) =>
    v === null || v === undefined
      ? <span className="badge">未对账</span>
      : <span className={`badge ${v ? "ok" : "bad"}`}>{v ? "valid" : "invalid"}</span>;

  return (
    <>
      <div className="topbar">
        <div>
          <h1>核证</h1>
          <p>当前镜 hist_id / row_hash 必须与 is_current 历史行一致；批对账以 id 集全等为准（架构乙），失败须可观测。</p>
        </div>
      </div>

      <div className="panel">
        <h2>批三方对账核证（只读）</h2>
        <p className="muted">
          三段计数（快照实落 / changed / 新开 hist）+ id 集摘要 + valid；live 重算与持久对账记录互证。
          规则 {`B_ID_SET`}：changed 的 snapshot_id 集 == 新开 hist 来源集；计数碰巧相等但 id 错位判失败。
        </p>
        <div className="actions">
          <input
            value={batchId}
            onChange={(e) => setBatchId(e.target.value)}
            placeholder="batch_id，如 B-1a2b3c4d"
            style={{ minWidth: 260, padding: "0.4rem 0.6rem", borderRadius: 8, border: "1px solid var(--line)", background: "#0c2226", color: "var(--text)" }}
          />
          <button onClick={check} disabled={!batchId.trim()}>核证该批</button>
          <button className="secondary" onClick={reload}>刷新</button>
        </div>
        {err && <p className="error">{err}</p>}
        {verify && (
          <div>
            <div className="grid-cards">
              <div className="card"><div className="label">快照实落数</div><div className="value">{verify.counts.snapshot}</div></div>
              <div className="card"><div className="label">changed 成员数</div><div className="value">{verify.counts.changed ?? "—"}</div></div>
              <div className="card"><div className="label">成功新开 hist 数</div><div className="value">{verify.counts.opened ?? "—"}</div></div>
              <div className="card">
                <div className="label">valid（乙：id 集全等）</div>
                <div className="value">{validBadge(verify.valid)}</div>
              </div>
              <div className="card">
                <div className="label">批次应用态</div>
                <div className="value">
                  <span className={`badge ${verify.apply_status === "applied" ? "ok" : verify.apply_status === "failed" ? "bad" : ""}`}>
                    {verify.apply_status}
                  </span>
                </div>
              </div>
              <div className="card">
                <div className="label">核证（记录 ↔ live）</div>
                <div className="value">{validBadge(verify.attestation.ok)}</div>
              </div>
            </div>
            {verify.id_sets && (
              <table>
                <thead><tr><th>id 集摘要</th><th>成员</th></tr></thead>
                <tbody>
                  <tr><td>changed snapshot_id 集</td><td className="mono">{verify.id_sets.changed.join(", ") || "∅"}</td></tr>
                  <tr><td>新开 hist 来源集</td><td className="mono">{verify.id_sets.opened.join(", ") || "∅"}</td></tr>
                  <tr><td>检测多报未开段</td><td className="mono">{verify.id_sets.missing_opened.join(", ") || "∅"}</td></tr>
                  <tr><td>hist 新开检测无成员</td><td className="mono">{verify.id_sets.extra_opened.join(", ") || "∅"}</td></tr>
                  <tr><td>跨批外来成员</td><td className="mono">{verify.id_sets.foreign_changed.join(", ") || "∅"}</td></tr>
                  <tr>
                    <td>口径对照</td>
                    <td className="mono">
                      甲·三段计数关系={String(verify.counts_relation_ok)}（仅展示） · 乙·id 集全等={String(verify.id_set_equal)}（valid 判据）
                    </td>
                  </tr>
                </tbody>
              </table>
            )}
            <table>
              <thead><tr><th>核证检查</th><th>结果</th></tr></thead>
              <tbody>
                {verify.attestation.checks.map((c) => (
                  <tr key={c.check}>
                    <td className="mono">{c.check}</td>
                    <td><span className={`badge ${c.ok ? "ok" : "bad"}`}>{c.ok ? "通过" : "失败"}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <pre className="msg">{JSON.stringify({ live: verify.live, record: verify.record }, null, 2)}</pre>
          </div>
        )}
      </div>

      <div className="panel">
        <h2>对账记录 batch_reconcile（近 50）</h2>
        <table>
          <thead>
            <tr>
              <th>对账单号</th><th>批</th><th>检测跑次</th><th>三段计数(落/变/开)</th>
              <th>甲·计数关系</th><th>乙·id 集全等</th><th>valid</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {recs.map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.id}</td>
                <td className="mono">{r.batch_id}</td>
                <td className="mono">{r.detect_run_id}</td>
                <td>{r.snapshot_count} / {r.changed_count} / {r.opened_count}</td>
                <td>{r.counts_relation_ok ? "满足" : "不满足"}</td>
                <td>{r.id_set_equal ? "全等" : "错位"}</td>
                <td>{validBadge(r.valid)}</td>
                <td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <div className="actions">
          <button onClick={rebuild}>重建镜证</button>
        </div>
        {msg && <pre className="msg">{msg}</pre>}
        <h2>scd_proofs（镜 ↔ 当前历史行）</h2>
        <table>
          <thead>
            <tr>
              <th>id</th><th>门店</th><th>hist</th><th>mirror_hist</th><th>hash</th><th>valid</th><th>detail</th><th>时间</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td>{r.store_id}</td>
                <td className="mono">{r.hist_id}</td>
                <td className="mono">{r.mirror_hist_id}</td>
                <td className="mono">{r.row_hash}</td>
                <td>
                  <span className={`badge ${r.valid ? "ok" : "bad"}`}>
                    {r.valid ? "通过" : "失败"}
                  </span>
                </td>
                <td className="mono">{r.detail_json}</td>
                <td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
