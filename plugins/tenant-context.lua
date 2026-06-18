--
-- tenant-context —— 租户上下文注入插件（本仓核心自定义插件）
--
-- 作用：从 openid-connect **验签后注入**的 X-Userinfo 中提取 org/tenant 声明，注入 X-Tenant-* 头下发上游，
--       并把租户暴露为 $tenant_id 变量，供 limit-count 等后续插件按租户维度使用。
--
-- 🔒 安全约束（违反即可能跨租户越权）：
--   本插件**必须**与 openid-connect 配置在同一路由、且在其后执行。租户头是下游做数据/计算隔离的可信根。
--   本插件不自行验签，而是消费 openid-connect 的验签产物 X-Userinfo：
--     · 该头由 openid-connect 在校验通过后注入，并会**覆盖**客户端伪造的同名头（已实测验证）；
--     · 任何**未配 openid-connect** 的路由上 X-Userinfo 不存在 → 本插件 fail-closed 拒绝（不注入任何租户头）。
--   进入即清除客户端可能携带的 X-Tenant-*，再写入网关可信值。
--
local core = require("apisix.core")
local ngx  = ngx

local plugin_name = "tenant-context"

local schema = {
    type = "object",
    properties = {
        userinfo_header  = { type = "string",  default = "X-Userinfo" },   -- 须与 openid-connect 的 set_userinfo_header 对齐
        tenant_claim     = { type = "string",  default = "organization" },
        fallback_claim   = { type = "string",  default = "tenant" },
        id_header        = { type = "string",  default = "X-Tenant-Id" },
        org_header       = { type = "string",  default = "X-Tenant-Org" },
        subject_header   = { type = "string",  default = "X-Tenant-Subject" },
        require_tenant   = { type = "boolean", default = true },
        max_userinfo_len = { type = "integer", default = 16384 },           -- 防超长头的 DoS 兜底
    },
}

local _M = {
    version  = 0.1,
    priority = 2598,   -- 紧随 openid-connect(2599) 之后执行
    name     = plugin_name,
    schema   = schema,
}

-- 暴露 $tenant_id：供 limit-count 等以 key_type=var、key=tenant_id 按租户维度取用。
-- 必须用注入后的可信变量，而非 $http_x_tenant_id（后者读的是客户端原始头、会被缓存）。
core.ctx.register_var("tenant_id", function(ctx)
    return ctx and ctx.tenant_id
end)

function _M.check_schema(conf)
    return core.schema.check(schema, conf)
end

local function decode_userinfo(b64, max_len)
    if #b64 > max_len then
        return nil, "userinfo header too large"
    end
    local raw = ngx.decode_base64(b64)
    if not raw then
        -- 容错：openid-connect 用标准 base64；个别实现可能是 base64url
        local s = b64:gsub("-", "+"):gsub("_", "/")
        local rem = #s % 4
        if rem > 0 then s = s .. string.rep("=", 4 - rem) end
        raw = ngx.decode_base64(s)
    end
    if not raw then
        return nil, "invalid base64 userinfo"
    end
    local info = core.json.decode(raw)
    if type(info) ~= "table" then
        return nil, "invalid JSON userinfo"
    end
    return info
end

-- 确定性解析租户：兼容 字符串 "acme" / 单元素数组 ["acme"] / 单键对象 {"acme": {...}}。
-- 多 org 成员视为歧义（拒绝），避免 pairs 顺序不确定带来的不可预测注入。
local function resolve_tenant(info, conf)
    local claim = info[conf.tenant_claim]
    local t = type(claim)
    if t == "string" then
        return claim
    elseif t == "table" then
        if claim[1] ~= nil then
            if #claim ~= 1 then
                return nil, "ambiguous multi-org membership"
            end
            return claim[1]
        end
        -- 对象形态：统计键数，>1 视为歧义；优先取 alias/name 子字段，避免返回 org id/UUID 键
        local only_key, only_val, count = nil, nil, 0
        for k, v in pairs(claim) do
            count = count + 1
            if count == 1 then only_key, only_val = k, v end
        end
        if count ~= 1 then
            return nil, "ambiguous multi-org membership"
        end
        if type(only_val) == "table" then
            return only_val.alias or only_val.name or only_key
        end
        return only_key
    end
    -- 回退到扁平声明
    local fb = info[conf.fallback_claim]
    if type(fb) == "string" then
        return fb
    end
    return nil, "no tenant claim"
end

function _M.rewrite(conf, ctx)
    -- 1) 清除客户端伪造的租户头（防越权）
    core.request.set_header(ctx, conf.id_header, nil)
    core.request.set_header(ctx, conf.org_header, nil)
    core.request.set_header(ctx, conf.subject_header, nil)

    -- 2) 读取 openid-connect 验签后注入的 userinfo；缺失 = 身份未由网关建立 → fail-closed
    local userinfo_b64 = core.request.header(ctx, conf.userinfo_header)
    if not userinfo_b64 then
        core.log.warn(plugin_name, ": missing ", conf.userinfo_header,
                      " — 该路由是否漏配 openid-connect？")
        if conf.require_tenant then
            return 401, { message = "identity not established (openid-connect required)" }
        end
        return
    end

    local info, err = decode_userinfo(userinfo_b64, conf.max_userinfo_len)
    if not info then
        core.log.warn(plugin_name, ": ", err)
        if conf.require_tenant then
            return 403, { message = "cannot resolve tenant context" }
        end
        return
    end

    -- 3) 解析租户并注入网关可信头 + 暴露 $tenant_id
    local tenant, terr = resolve_tenant(info, conf)
    if not tenant then
        core.log.warn(plugin_name, ": ", terr)
        if conf.require_tenant then
            return 403, { message = "no tenant claim in identity" }
        end
        return
    end

    core.request.set_header(ctx, conf.org_header, tenant)
    core.request.set_header(ctx, conf.id_header, tenant)   -- demo：alias 即 id；生产可映射 org UUID
    if info.sub then
        core.request.set_header(ctx, conf.subject_header, info.sub)
    end
    ctx.tenant_id = tenant
end

return _M
