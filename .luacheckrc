-- APISIX 自定义插件运行于 OpenResty/LuaJIT；声明全局以避免误报。
std = "luajit"
globals = { "ngx" }
max_line_length = false
unused_args = false
