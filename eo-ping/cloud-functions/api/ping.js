// 国内延迟探针（EdgeOne Pages · Node.js 云函数）
//
// 通过项目根 edgeone.json 的 cloudFunctions.mainlandRegions 钉死到国内地域，
// 因此无论从哪里（哪怕是 GitHub Actions 美国 runner）调用，函数实体都在国内执行，
// 对目标站 fetch 走国内出口 => 返回的是"国内视角"延迟。
// 该值与 main.py 测得的美国视角 latency 并存对照（见 check-flink/main.py 的 latency_cn 字段）。
//
// 触发： GET /api/ping?url=<目标站地址>
// 返回： { ok, url, status, latency(秒), latency_ms, error? }
//
// 说明：
// - 仅测到 Response 头（TTFB 量级）即记录耗时，不消费整个 body，足够反映连通延迟。
// - 内置 SSRF 防护：仅允许公网 http/https，阻断内网/保留地址，避免本端点被当作内网代理。

export async function onRequest({ request }) {
  const cors = { 'Access-Control-Allow-Origin': '*' };
  const json = (body, status = 200) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json; charset=UTF-8', ...cors },
    });

  // 解析目标地址
  let target;
  try {
    target = new URL(request.url).searchParams.get('url');
  } catch {
    return json({ ok: false, error: 'bad request' }, 400);
  }
  if (!target) return json({ ok: false, error: 'missing url param' }, 400);

  // SSRF 防护：仅允许公网 http/https，阻断内网/保留地址
  let parsed;
  try {
    parsed = new URL(target);
  } catch {
    return json({ ok: false, error: 'invalid target url' }, 400);
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return json({ ok: false, error: 'only http/https allowed' }, 400);
  }
  const host = parsed.hostname.toLowerCase();
  const blocked =
    host === 'localhost' || host.endsWith('.localhost') ||
    host === '0.0.0.0' || /^127\./.test(host) ||
    /^10\./.test(host) || /^192\.168\./.test(host) ||
    /^172\.(1[6-9]|2\d|3[01])\./.test(host) ||
    host.startsWith('169.254.') ||
    /^100\.(6[4-9]|[7-9]\d|1[0-2]\d)\./.test(host) ||
    host.endsWith('.internal') || host.endsWith('.local') || host.endsWith('.svc');
  if (blocked) return json({ ok: false, error: 'blocked host' }, 403);

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);
  const start = Date.now();
  try {
    const resp = await fetch(target, {
      method: 'GET',
      redirect: 'follow',
      signal: controller.signal,
      headers: {
        'User-Agent':
          'Mozilla/5.0 (compatible; check-flink-cn-probe/1.0; +https://github.com/willow-god/check-flink)',
        'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
      },
    });
    const latencyMs = Date.now() - start;
    return json({
      ok: true,
      url: target,
      status: resp.status,
      // 秒，2 位小数，与 main.py 的 latency 字段单位一致
      latency: Math.round((latencyMs / 1000) * 100) / 100,
      latency_ms: latencyMs,
    });
  } catch (e) {
    const latencyMs = Date.now() - start;
    const aborted = e && e.name === 'AbortError';
    return json(
      {
        ok: false,
        url: target,
        latency: -1,
        latency_ms: latencyMs,
        error: aborted ? 'timeout' : String((e && e.message) || e),
      },
      aborted ? 504 : 502,
    );
  } finally {
    clearTimeout(timer);
  }
}
