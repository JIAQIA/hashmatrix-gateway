--
-- audit-log —— 审计日志插件（自定义示例）
--
-- 在 log 阶段输出结构化审计行（方法/路径/状态/租户/主体/耗时/客户端 IP）。
-- 仅用 core.log 输出到 APISIX 日志(stdout)，零外部依赖；生产可替换为 http-logger / kafka-logger。
--
local core = require("apisix.core")
local ngx  = ngx

local schema = {
    type = "object",
    properties = {
        log_level = { type = "string", enum = { "info", "warn", "error" }, default = "info" },
    },
}

local _M = {
    version  = 0.1,
    priority = 397,
    name     = "audit-log",
    schema   = schema,
}

function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end

function _M.log(conf, ctx)
    local entry = {
        event      = "gateway.audit",
        method     = ngx.var.request_method,
        uri        = ngx.var.request_uri,
        status     = ngx.status,
        tenant_id  = core.request.header(ctx, "X-Tenant-Id"),
        tenant_org = core.request.header(ctx, "X-Tenant-Org"),
        subject    = core.request.header(ctx, "X-Tenant-Subject"),
        client_ip  = ngx.var.remote_addr,
        latency_ms = (ngx.now() - ngx.req.start_time()) * 1000,
    }
    local line = "[audit] " .. core.json.encode(entry)
    if conf.log_level == "error" then
        core.log.error(line)
    elseif conf.log_level == "info" then
        core.log.info(line)
    else
        core.log.warn(line)
    end
end

return _M
