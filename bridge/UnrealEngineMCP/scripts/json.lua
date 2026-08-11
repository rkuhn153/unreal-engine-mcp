-- Minimal JSON encode/decode for UnrealEngineMCP bridge (Lua 5.4 / UE4SS).
-- Handles the subset we use: objects, arrays, strings, numbers, bools, null.

local json = {}

local function escape_str(s)
    s = tostring(s)
    s = s:gsub('\\', '\\\\')
    s = s:gsub('"', '\\"')
    s = s:gsub('\b', '\\b')
    s = s:gsub('\f', '\\f')
    s = s:gsub('\n', '\\n')
    s = s:gsub('\r', '\\r')
    s = s:gsub('\t', '\\t')
    return '"' .. s .. '"'
end

local function is_array(t)
    if type(t) ~= "table" then
        return false
    end
    local n = 0
    for k, _ in pairs(t) do
        if type(k) ~= "number" then
            return false
        end
        if k > n then
            n = k
        end
    end
    for i = 1, n do
        if t[i] == nil then
            return false
        end
    end
    return true
end

function json.encode(val)
    local t = type(val)
    if val == nil then
        return "null"
    elseif t == "boolean" then
        return val and "true" or "false"
    elseif t == "number" then
        if val ~= val or val == math.huge or val == -math.huge then
            return "null"
        end
        return string.format("%.14g", val)
    elseif t == "string" then
        return escape_str(val)
    elseif t == "table" then
        if is_array(val) then
            local parts = {}
            for i = 1, #val do
                parts[#parts + 1] = json.encode(val[i])
            end
            return "[" .. table.concat(parts, ",") .. "]"
        else
            local parts = {}
            for k, v in pairs(val) do
                if type(k) == "string" then
                    parts[#parts + 1] = escape_str(k) .. ":" .. json.encode(v)
                end
            end
            return "{" .. table.concat(parts, ",") .. "}"
        end
    else
        return escape_str(tostring(val))
    end
end

local function skip_ws(str, i)
    local _, j = str:find("^[ \t\r\n]*", i)
    return (j or i - 1) + 1
end

local function parse_value(str, i)
    i = skip_ws(str, i)
    local c = str:sub(i, i)
    if c == '"' then
        local out = {}
        i = i + 1
        while i <= #str do
            local ch = str:sub(i, i)
            if ch == '"' then
                return table.concat(out), i + 1
            elseif ch == '\\' then
                local n = str:sub(i + 1, i + 1)
                local map = { b = "\b", f = "\f", n = "\n", r = "\r", t = "\t", ['"'] = '"', ["\\"] = "\\", ["/"] = "/" }
                if n == "u" then
                    local hex = str:sub(i + 2, i + 5)
                    local code = tonumber(hex, 16) or 0
                    if code < 128 then
                        out[#out + 1] = string.char(code)
                    else
                        out[#out + 1] = "?"
                    end
                    i = i + 6
                else
                    out[#out + 1] = map[n] or n
                    i = i + 2
                end
            else
                out[#out + 1] = ch
                i = i + 1
            end
        end
        error("unterminated string")
    elseif c == "{" then
        local obj = {}
        i = i + 1
        i = skip_ws(str, i)
        if str:sub(i, i) == "}" then
            return obj, i + 1
        end
        while true do
            i = skip_ws(str, i)
            local key
            key, i = parse_value(str, i)
            i = skip_ws(str, i)
            if str:sub(i, i) ~= ":" then
                error("expected :")
            end
            i = i + 1
            local val
            val, i = parse_value(str, i)
            obj[key] = val
            i = skip_ws(str, i)
            local sep = str:sub(i, i)
            if sep == "}" then
                return obj, i + 1
            elseif sep == "," then
                i = i + 1
            else
                error("expected , or }")
            end
        end
    elseif c == "[" then
        local arr = {}
        i = i + 1
        i = skip_ws(str, i)
        if str:sub(i, i) == "]" then
            return arr, i + 1
        end
        while true do
            local val
            val, i = parse_value(str, i)
            arr[#arr + 1] = val
            i = skip_ws(str, i)
            local sep = str:sub(i, i)
            if sep == "]" then
                return arr, i + 1
            elseif sep == "," then
                i = i + 1
            else
                error("expected , or ]")
            end
        end
    elseif str:sub(i, i + 3) == "true" then
        return true, i + 4
    elseif str:sub(i, i + 4) == "false" then
        return false, i + 5
    elseif str:sub(i, i + 3) == "null" then
        return nil, i + 4
    else
        local num = str:match("^%-?%d+%.?%d*[eE]?[%+%-]?%d*", i)
        if not num then
            error("invalid token at " .. i)
        end
        return tonumber(num), i + #num
    end
end

function json.decode(str)
    if type(str) ~= "string" then
        error("json.decode expects string")
    end
    local val, i = parse_value(str, 1)
    i = skip_ws(str, i)
    if i <= #str then
        -- trailing junk ignored for robustness
    end
    return val
end

return json
