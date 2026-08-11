--[[
  UnrealEngineMCP 3.3.0-beta.1 — UE4SS Lua bridge (SemVer 2.0.0)

  Versioning (https://semver.org/):
    MAJOR.MINOR.PATCH[-prerelease]
    This release is beta: usable on known titles, not a stability / API lock (not rc / 1.0).

  Transport: file IPC next to the shipping Win64 binary
    UnrealEngineMCP_IPC/
      request.json + request.flag  (Python -> game)
      response.json + response.flag (game -> Python)
      heartbeat.json               (game liveness)
      revive.flag                  (MCP / FatalGuard mid-game pump revive)

  Engine roots / offsets: owned by UE4SS signature scan. This mod only uses
  reflection APIs (FindAllOf, GetPropertyValue, UFunction calls, etc.).

  Feature history (pre-SemVer labels collapsed into 3.3.0-beta.1):
    - Recoverable single-tick pump (no dual ExecuteWithDelay — UE4SS #1180)
    - Mid-game revive (revive.flag + force_revive + optional FatalGuard autokick)
    - Serialization / call safety (TArray shape, Debug/RPC refuse, arity checks)
    - Gear loadout (EquippedGear + GearSlots; light reads by default)
    - BFBB null-ref hardening; GS3 no_hooks / invalid UObject guards
]]

local json = require("json")

local MOD_NAME = "UnrealEngineMCP"
-- SemVer 2.0.0 package + IPC protocol string reported on ping/status/heartbeat
local PROTOCOL_VERSION = "3.3.0-beta.1"
local PUMP_ID = "recoverable"
local ARRAY_MAX_ITEMS = 64

-- Common / guessed fields on GG gear structs (safe name probes only)
local STRUCT_FIELD_CANDIDATES = {
    "Gear", "GearClass", "EquippedGear", "EquippedClass", "SoftGearClass",
    "GearDataAsset", "DataAsset", "Data", "Definition", "Item", "ItemClass",
    "Slot", "SlotType", "GearSlot", "SlotId", "SlotID", "GearSlotType", "ESlot",
    "AssetId", "AssetID", "StyleId", "StyleID", "Style",
    "bEmpty", "IsEmpty", "bIsEquipped", "bEquipped", "bHasGear",
    "CurrentGear", "GearActor", "Actor", "Class", "Part", "GearPart",
    "Type", "Name", "DisplayName", "ID", "Id", "Tag", "Tags",
}
-- Gentle defaults: single tick, long boot warmup (BFBB DXGI assert under stress).
local TICK_MS = 250
local BOOT_WARMUP_MS = 45000
local POST_MAP_WARMUP_MS = 8000
local BUSY_STUCK_SEC = 5
local SAMPLE_CHUNK = 8

local ipc_dir = nil
local poll_count = 0
local work_count = 0
local started_at = os.time()
local busy = false
local busy_since = 0
local last_cmd = nil
local last_error = nil
local last_hb_wall = os.time()
local last_work_wall = os.time()
local bridge_ready = false
local pump_gen = 0
local game_state_suspect = false
local jobs = {} -- id -> job state for multi-tick heavy walks

local function log(msg)
    print(string.format("[%s] %s\n", MOD_NAME, tostring(msg)))
end

local function safe_require_helpers()
    local ok, helpers = pcall(function()
        return require("UEHelpers")
    end)
    if ok then
        return helpers
    end
    return nil
end

local UEHelpers = safe_require_helpers()

local function path_join(a, b)
    if a:sub(-1) == "\\" or a:sub(-1) == "/" then
        return a .. b
    end
    return a .. "\\" .. b
end

local function detect_ipc_dir()
    -- Prefer Win64 next to the shipping binary when IterateGameDirectories works.
    local ok, dirs = pcall(function()
        return IterateGameDirectories()
    end)
    if ok and dirs and dirs.Game and dirs.Game.Binaries and dirs.Game.Binaries.Win64 then
        local win64 = dirs.Game.Binaries.Win64
        local abs = win64.__absolute_path
        if abs and #abs > 0 then
            return path_join(abs, "UnrealEngineMCP_IPC")
        end
    end
    -- Fallback: working directory style path (still usually Win64 when UE4SS loads).
    return "UnrealEngineMCP_IPC"
end

local function ensure_dir(dir)
    -- UE4SS Lua has no built-in mkdir; try via io and ignore failures if exists.
    -- Parent process Install-ToGame.ps1 creates this; we also try a no-op open.
    local test = io.open(path_join(dir, ".keep"), "a")
    if test then
        test:close()
        return true
    end
    return false
end

local function read_file(path)
    local f = io.open(path, "rb")
    if not f then
        return nil
    end
    local data = f:read("*a")
    f:close()
    return data
end

local function write_file(path, data)
    local f = io.open(path, "wb")
    if not f then
        return false
    end
    f:write(data or "")
    f:close()
    return true
end

local function delete_file(path)
    os.remove(path)
end

local function file_exists(path)
    local f = io.open(path, "rb")
    if f then
        f:close()
        return true
    end
    return false
end

-- Forward declare for TArray <-> element recursion.
local serialize_value

-- Summarize UObject / UClass.
-- CRITICAL: IsValid()==false → do NOT call GetFullName/GetClass/etc (native AV on GS3 gear).
local function serialize_uobject(val)
    if val == nil then
        return nil
    end

    local is_valid = nil
    local ok_iv = pcall(function()
        if val.IsValid then
            is_valid = val:IsValid() and true or false
        end
    end)
    if not ok_iv then
        return { __type = "InvalidObject", note = "IsValid check failed" }
    end

    -- Stale / null soft refs: stop here. pcall does NOT catch native AVs.
    if is_valid == false then
        return {
            __type = "InvalidObject",
            is_valid = false,
            note = "skipped UObject methods on IsValid=false (crash-safe)",
        }
    end

    local full, short, class_name, addr = nil, nil, nil, nil

    pcall(function()
        if val.GetFullName then
            full = val:GetFullName()
        end
    end)
    pcall(function()
        if val.GetName then
            short = val:GetName()
        elseif val.GetFName then
            local fn = val:GetFName()
            if fn and fn.ToString then
                short = fn:ToString()
            end
        end
    end)
    pcall(function()
        if val.GetPathName then
            local path = val:GetPathName()
            if path and (not full or full == "") then
                full = path
            end
        end
    end)
    pcall(function()
        if val.GetAddress then
            addr = string.format("0x%X", val:GetAddress())
        end
    end)
    pcall(function()
        local c = val.GetClass and val:GetClass()
        if c then
            if c.GetFullName then
                class_name = c:GetFullName()
            elseif c.GetName then
                class_name = c:GetName()
            end
        end
    end)

    -- ScriptStruct *type* objects are not useful gear instances (GearSlots bug)
    if type(full) == "string" and full:match("^ScriptStruct%s") then
        return nil
    end
    if type(class_name) == "string" and class_name:match("ScriptStruct") and (not full or full == "") then
        return nil
    end

    if full or short or class_name then
        local out = {
            __type = "UObject",
            full_name = full,
            name = short,
            address = addr,
            class = class_name,
            is_valid = true,
        }
        if short and short ~= "" then
            out.id = short
        elseif full and type(full) == "string" then
            local last = full:match("([^%s./]+)$")
            if last then
                out.id = last
            end
        end
        return out
    end
    return nil
end

local function is_useful_gear_ref(ser)
    if type(ser) ~= "table" then
        return false
    end
    if ser.__type == "InvalidObject" or ser.__type == "Error" then
        return false
    end
    if ser.full_name and type(ser.full_name) == "string" then
        if ser.full_name:match("^ScriptStruct%s") then
            return false
        end
        if ser.full_name:match("Gear") or ser.full_name:match("BP_") or ser.id then
            return true
        end
    end
    if ser.id and type(ser.id) == "string" and ser.id ~= "Manager" then
        return true
    end
    if ser.__type == "Struct" and ser.field_count and ser.field_count > 0 then
        return true
    end
    return ser.__type == "UObject" and ser.is_valid == true
end

-- Expand a struct *instance* by probing fields.
-- deep=false (default): only Lua table keys + a few known string/object field names.
-- deep=true: full candidate list + ForEachProperty (more side-effect risk on soft refs).
local function serialize_struct_fields(val, depth, deep)
    depth = depth or 0
    deep = deep == true
    if val == nil or depth > 5 then
        return nil
    end
    local fields = {}
    local found = 0

    local function set_field(k, v)
        if k == nil or v == nil or fields[k] ~= nil then
            return
        end
        local ok, ser = pcall(function()
            return serialize_value(v, depth + 1)
        end)
        if not ok or ser == nil then
            return
        end
        if type(ser) == "table" and ser.full_name and ser.full_name:match("^ScriptStruct%s") then
            return
        end
        if type(ser) == "table" and ser.__type == "InvalidObject" then
            return
        end
        fields[k] = ser
        found = found + 1
    end

    -- A) Lua table export of struct (usually non-destructive)
    if type(val) == "table" then
        for k, v in pairs(val) do
            if type(k) == "string" and not tostring(k):match("^__") and found < 40 then
                set_field(k, v)
            end
        end
    end

    -- B) Limited known names (avoid blasting every guess — soft ptr resolution risk)
    local names = deep and STRUCT_FIELD_CANDIDATES or {
        "Gear", "GearClass", "EquippedClass", "SoftGearClass", "Class",
        "Slot", "SlotType", "GearSlot", "AssetId", "AssetID", "StyleId",
    }
    for _, fname in ipairs(names) do
        if found >= 40 then
            break
        end
        local ok, v = pcall(function()
            if val.GetPropertyValue then
                return val:GetPropertyValue(fname)
            end
            return val[fname]
        end)
        if ok and v ~= nil then
            set_field(fname, v)
        end
    end

    -- C) Full reflection only when deep=true
    if deep then
        pcall(function()
            local struct = nil
            if val.GetClass then
                struct = val:GetClass()
            end
            if not struct and val.GetScriptStruct then
                struct = val:GetScriptStruct()
            end
            if struct and struct.ForEachProperty then
                struct:ForEachProperty(function(prop)
                    if found >= 40 then
                        return
                    end
                    local pn = nil
                    pcall(function()
                        pn = prop:GetFName():ToString()
                    end)
                    if pn and fields[pn] == nil then
                        local ok, v = pcall(function()
                            if val.GetPropertyValue then
                                return val:GetPropertyValue(pn)
                            end
                            return val[pn]
                        end)
                        if ok and v ~= nil then
                            set_field(pn, v)
                        end
                    end
                end)
            end
        end)
    end

    if found == 0 then
        return nil
    end
    return {
        __type = "Struct",
        field_count = found,
        fields = fields,
        deep = deep,
    }
end

-- Light element read: avoid ForEach param :get() when possible (save dirty risk).
local function serialize_array_element_light(elem, depth)
    depth = depth or 0
    if elem == nil then
        return { __type = "InvalidObject", note = "nil" }
    end
    -- Direct UObject/UClass (hard refs) — IsValid then names only
    local ok_uo, uo = pcall(function()
        return serialize_uobject(elem)
    end)
    if ok_uo and is_useful_gear_ref(uo) then
        return uo
    end
    -- Table-shaped struct (no GetPropertyValue storm)
    if type(elem) == "table" then
        local st = serialize_struct_fields(elem, depth, false)
        if st then
            return st
        end
    end
    if ok_uo and uo then
        return uo
    end
    return { __type = "InvalidObject", note = "light read: no name (not probing soft get())" }
end

local function serialize_array_element(elem, depth, deep)
    depth = depth or 0
    deep = deep == true
    if not deep then
        return serialize_array_element_light(elem, depth)
    end
    -- deep path: allow get() / more field probes (may affect soft refs / save)
    local candidates = { elem }
    pcall(function()
        if elem and elem.get then
            candidates[#candidates + 1] = elem:get()
        end
    end)
    pcall(function()
        if elem and elem.Get then
            candidates[#candidates + 1] = elem:Get()
        end
    end)

    local best_uo, best_struct = nil, nil
    for _, c in ipairs(candidates) do
        if c ~= nil then
            local ok_uo, uo = pcall(function()
                return serialize_uobject(c)
            end)
            if ok_uo and is_useful_gear_ref(uo) then
                best_uo = uo
                break
            end
            if ok_uo and uo and not best_uo then
                best_uo = uo
            end
            local ok_st, st = pcall(function()
                return serialize_struct_fields(c, depth, true)
            end)
            if ok_st and st and st.field_count and st.field_count > 0 then
                best_struct = st
                local f = st.fields or {}
                if f.Gear or f.GearClass or f.EquippedClass or f.Class or f.SoftGearClass
                    or f.DataAsset or f.Slot or f.SlotType then
                    break
                end
            end
        end
    end

    if best_struct and best_struct.field_count and best_struct.field_count > 0 then
        if best_uo and is_useful_gear_ref(best_uo) then
            best_struct.object = best_uo
        end
        return best_struct
    end
    if best_uo then
        return best_uo
    end
    return { __type = "InvalidObject", note = "element had no readable fields" }
end

local function is_numeric_vector_like(val)
    -- Only treat as FVector when X/Y/Z are actual numbers (TArray userdata
    -- sometimes exposes .X/.Y/.Z as first elements — that crashed gear dumps).
    local ok, is_vec = pcall(function()
        local x, y, z = val.X, val.Y, val.Z
        if type(x) ~= "number" or type(y) ~= "number" or type(z) ~= "number" then
            return false
        end
        -- Reject if it also looks like a sequence/array
        if type(val) == "table" and #val > 0 then
            return false
        end
        local ok_num, n = pcall(function()
            if val.GetArrayNum then
                return val:GetArrayNum()
            end
            return nil
        end)
        if ok_num and type(n) == "number" and n > 0 then
            return false
        end
        return true
    end)
    return ok and is_vec
end

local function looks_like_uobject(val)
    if type(val) ~= "userdata" and type(val) ~= "table" then
        return false
    end
    local ok, yes = pcall(function()
        -- TArray tostring is "TArray: 0x..." — never treat as UObject
        local ts = nil
        if val.ToString then
            ts = val:ToString()
        end
        if type(ts) ~= "string" then
            ts = tostring(val)
        end
        if type(ts) == "string" and ts:match("^TArray") then
            return false
        end
        -- Prefer method call over field presence
        local ok_n, n = pcall(function()
            return val:GetArrayNum()
        end)
        if ok_n and type(n) == "number" then
            return false
        end
        if val.GetFullName or val.GetClass then
            return true
        end
        return false
    end)
    return ok and yes
end

local function unwrap_array_elem(elem)
    if elem == nil then
        return nil
    end
    -- UE4SS RemoteUnrealParam / LocalUnrealParam — try several access styles
    local candidates = {}
    pcall(function()
        if elem.get then
            candidates[#candidates + 1] = elem:get()
        end
    end)
    pcall(function()
        if elem.Get then
            candidates[#candidates + 1] = elem:Get()
        end
    end)
    pcall(function()
        if elem.GetValue then
            candidates[#candidates + 1] = elem:GetValue()
        end
    end)
    pcall(function()
        -- Some bindings expose the value as a property
        if elem.Value ~= nil then
            candidates[#candidates + 1] = elem.Value
        end
    end)
    candidates[#candidates + 1] = elem

    for _, c in ipairs(candidates) do
        if c ~= nil then
            -- Prefer something that can produce a name even if IsValid is false
            local ok_v, valid = pcall(function()
                if c.IsValid then
                    return c:IsValid()
                end
                return true
            end)
            if ok_v and valid then
                return c
            end
        end
    end
    -- Fall back to first non-nil (may be soft/stale — serialize_uobject will try names)
    for _, c in ipairs(candidates) do
        if c ~= nil then
            return c
        end
    end
    return elem
end

local function try_array_len(val)
    if looks_like_uobject(val) then
        return nil, nil
    end

    -- Call method (field check is often nil on userdata metamethods)
    local ok, n = pcall(function()
        return val:GetArrayNum()
    end)
    if ok and type(n) == "number" and n >= 0 then
        return n, "GetArrayNum"
    end

    -- Detect by tostring even if GetArrayNum failed
    local ts = nil
    pcall(function()
        ts = tostring(val)
    end)
    if type(ts) == "string" and ts:match("^TArray") then
        -- Try capacity-based / index until nil, max ARRAY_MAX_ITEMS
        local count = 0
        for i = 1, ARRAY_MAX_ITEMS do
            local ok_i, elem = pcall(function()
                return val[i]
            end)
            if not ok_i or elem == nil then
                break
            end
            count = i
        end
        -- 0-length TArray is valid
        return count, "TArray_index"
    end

    if type(val) == "table" then
        local len = rawlen and rawlen(val) or #val
        if type(len) == "number" and len > 0 then
            local numeric = 0
            local total = 0
            for k, _ in pairs(val) do
                total = total + 1
                if type(k) == "number" then
                    numeric = numeric + 1
                end
                if total > 8 then
                    break
                end
            end
            if numeric >= math.min(total, 1) and numeric >= total * 0.5 then
                return len, "lua_len"
            end
        end
        local max_i, count = 0, 0
        for k, _ in pairs(val) do
            if type(k) == "number" and k >= 1 and k == math.floor(k) then
                count = count + 1
                if k > max_i then
                    max_i = k
                end
            end
        end
        if count > 0 and max_i <= count + 2 then
            return max_i, "numeric_keys"
        end
        local ok_fake, fake_n = pcall(function()
            local x, y, z = val.X, val.Y, val.Z
            if x == nil or y == nil or z == nil then
                return nil
            end
            if type(x) == "number" and type(y) == "number" and type(z) == "number" then
                return nil
            end
            return 3
        end)
        if ok_fake and fake_n == 3 then
            return 3, "xyz_as_array"
        end
    end
    return nil, nil
end

local function array_elem_at(val, i, source)
    if source == "xyz_as_array" then
        local keys = { "X", "Y", "Z" }
        local k = keys[i]
        if not k then
            return nil
        end
        local ok, e = pcall(function()
            return val[k]
        end)
        if ok then
            return unwrap_array_elem(e)
        end
        return nil
    end
    -- UE4SS TArray is often 0-based via Get; try 1-based then 0-based
    local ok, e = pcall(function()
        return val[i]
    end)
    if ok and e ~= nil then
        return unwrap_array_elem(e)
    end
    ok, e = pcall(function()
        return val[i - 1]
    end)
    if ok and e ~= nil then
        return unwrap_array_elem(e)
    end
    return nil
end

-- mode: "light" (default) = GetArrayNum+index, no ForEach/:get
--       "deep" = ForEach + get() + deep struct probes (more complete, save-risk)
local function serialize_array(val, depth, max_items, mode)
    max_items = max_items or ARRAY_MAX_ITEMS
    mode = mode or "light"
    local deep = mode == "deep"

    -- LIGHT PATH FIRST: index walk only (less likely to dirty soft/array proxies)
    local n, how = try_array_len(val)
    if n ~= nil then
        local items = {}
        local limit = math.min(n, max_items)
        for i = 1, limit do
            local ok_e, elem = pcall(function()
                return array_elem_at(val, i, how)
            end)
            if ok_e and elem ~= nil then
                local ok_s, ser = pcall(function()
                    return serialize_array_element(elem, depth + 1, deep)
                end)
                if ok_s and ser ~= nil then
                    items[#items + 1] = ser
                else
                    items[#items + 1] = { __type = "Error", error = "element serialize failed" }
                end
            elseif ok_e and elem == nil then
                items[#items + 1] = { __type = "InvalidObject", note = "nil slot" }
            else
                items[#items + 1] = { __type = "Error", error = "index failed" }
            end
        end
        return {
            __type = "Array",
            count = n,
            shown = #items,
            truncated = n > #items,
            source = how or "index",
            mode = mode,
            items = items,
        }
    end

    if not deep then
        return nil
    end

    -- DEEP fallback: ForEach (only when light path could not size the array)
    local foreach_items = {}
    local foreach_ok = pcall(function()
        if not val.ForEach then
            error("no ForEach")
        end
        val:ForEach(function(idx, elem)
            if #foreach_items >= max_items then
                return
            end
            local ser = nil
            local ok_s, res = pcall(function()
                return serialize_array_element(elem, depth + 1, true)
            end)
            if ok_s then
                ser = res
            else
                ser = { __type = "Error", error = "serialize_array_element failed" }
            end
            if ser == nil then
                ser = { __type = "InvalidObject", note = "nil element" }
            end
            foreach_items[#foreach_items + 1] = {
                index = idx,
                value = ser,
            }
        end)
    end)
    if foreach_ok and #foreach_items > 0 then
        local flat = {}
        for _, row in ipairs(foreach_items) do
            flat[#flat + 1] = row.value
        end
        return {
            __type = "Array",
            count = #flat,
            shown = #flat,
            truncated = #flat >= max_items,
            source = "ForEach",
            mode = "deep",
            items = flat,
        }
    end
    return nil
end

serialize_value = function(val, depth)
    depth = depth or 0
    if depth > 4 then
        return "<max_depth>"
    end
    if val == nil then
        return nil
    end
    local t = type(val)
    if t == "boolean" or t == "number" or t == "string" then
        return val
    end
    if t ~= "userdata" and t ~= "table" then
        return tostring(val)
    end

    -- 1) UObject / UClass FIRST — never index-probe these as arrays (GS3 hang)
    if looks_like_uobject(val) then
        local uo = serialize_uobject(val)
        if uo then
            return uo
        end
        -- May be a struct instance that failed UObject summary (ScriptStruct type)
        local st = serialize_struct_fields(val, depth)
        if st then
            return st
        end
    end

    -- 2) TArray (GetArrayNum) or plain Lua lists — not UObject userdata
    local arr = serialize_array(val, depth, ARRAY_MAX_ITEMS)
    if arr then
        return arr
    end

    -- 3) Struct field expansion (GearSlot etc.)
    local st2 = serialize_struct_fields(val, depth)
    if st2 then
        return st2
    end

    -- 4) Non-UObject userdata that still has GetFullName (edge)
    local uo2 = serialize_uobject(val)
    if uo2 then
        return uo2
    end

    -- 5) FName / FString-ish
    local ok_ts, s = pcall(function()
        if val.ToString then
            return val:ToString()
        end
        return nil
    end)
    if ok_ts and type(s) == "string" and s ~= "" then
        if not s:match("^UClass:%s*") and not s:match("^UScriptStruct:%s*") then
            return s
        end
        return { __type = "OpaqueRef", tostring = s }
    end

    -- 6) True FVector (numeric only)
    if is_numeric_vector_like(val) then
        return { X = val.X, Y = val.Y, Z = val.Z, __type = "Vector" }
    end

    -- 7) Rotator
    local ok_rot, rot = pcall(function()
        local p, y, r = val.Pitch, val.Yaw, val.Roll
        if type(p) == "number" and type(y) == "number" and type(r) == "number" then
            return { Pitch = p, Yaw = y, Roll = r, __type = "Rotator" }
        end
        return nil
    end)
    if ok_rot and rot then
        return rot
    end

    -- 8) Generic table map
    if t == "table" then
        local out = { __type = "Map" }
        local count = 0
        for k, v in pairs(val) do
            count = count + 1
            if count > 48 then
                out["__truncated"] = true
                break
            end
            out[tostring(k)] = serialize_value(v, depth + 1)
        end
        out.__count = count
        return out
    end

    return { __type = "Unknown", tostring = tostring(val) }
end

local function coerce_input(val)
    -- Pass through JSON primitives; tables with X/Y/Z become vector-ish tables.
    if type(val) == "table" and val.X ~= nil and val.Y ~= nil and val.Z ~= nil then
        return { X = val.X, Y = val.Y, Z = val.Z }
    end
    return val
end

local function object_summary(obj)
    if not obj then
        return nil
    end
    local ok, valid = pcall(function()
        return obj:IsValid()
    end)
    if ok and not valid then
        return nil
    end
    local full_name, address, class_name, short_name = nil, nil, nil, nil
    pcall(function()
        full_name = obj:GetFullName()
    end)
    pcall(function()
        address = string.format("0x%X", obj:GetAddress())
    end)
    pcall(function()
        local c = obj:GetClass()
        if c then
            class_name = c:GetFullName()
            if c.GetFName then
                short_name = c:GetFName():ToString()
            end
        end
    end)
    local fname = nil
    pcall(function()
        fname = obj:GetFName():ToString()
    end)
    return {
        full_name = full_name,
        name = fname,
        address = address,
        class = class_name,
        class_short = short_name,
    }
end

local function parse_address(addr)
    if type(addr) == "number" then
        return addr
    end
    if type(addr) ~= "string" then
        return nil
    end
    local hex = addr:match("^0[xX](%x+)$")
    if hex then
        return tonumber(hex, 16)
    end
    if addr:match("^%d+$") then
        return tonumber(addr)
    end
    return nil
end

local function try_find_by_name(name)
    if not name or name == "" then
        return nil
    end
    local candidates = { name }
    -- GetFullName() => "ClassName /Game/Path.Object" — StaticFindObject often wants the path.
    local path_only = name:match("^%S+%s+(.+)$")
    if path_only and path_only ~= name then
        candidates[#candidates + 1] = path_only
    end
    -- Also try last path segment as short name via FindObject.
    local short = name:match("([^%.]+)$")
    if short then
        candidates[#candidates + 1] = short
    end

    for _, cand in ipairs(candidates) do
        local ok, obj = pcall(function()
            return StaticFindObject(cand)
        end)
        if ok and obj and obj.IsValid and obj:IsValid() then
            return obj
        end
        ok, obj = pcall(function()
            return FindObject(nil, cand)
        end)
        if ok and obj and obj.IsValid and obj:IsValid() then
            return obj
        end
    end
    return nil
end

local function resolve_object(params)
    params = params or {}

    if params.full_name and params.full_name ~= "" then
        local obj = try_find_by_name(params.full_name)
        if obj then
            return obj, nil
        end
        -- Fallback: match by full_name string among class instances when class_short known.
        local class_short = params.class or params.class_short
        if not class_short or class_short == "" then
            class_short = params.full_name:match("^(%S+)")
        end
        if class_short then
            local ok, objs = pcall(function()
                return FindAllOf(class_short)
            end)
            if ok and objs then
                if type(objs) == "table" then
                    for _, o in pairs(objs) do
                        local s = object_summary(o)
                        if s and s.full_name == params.full_name then
                            return o, nil
                        end
                        if s and s.address and params.address and s.address == params.address then
                            return o, nil
                        end
                    end
                elseif objs.IsValid and objs:IsValid() then
                    return objs, nil
                end
            end
        end
        return nil, "object not found for full_name"
    end

    if params.address and params.address ~= "" then
        local target = parse_address(params.address)
        if not target then
            return nil, "invalid address"
        end
        -- Address-only: auto limited scan (BFBB often has full_name for components but
        -- agents still pass address). Full 50k scan only when allow_address_scan=true.
        local max_scan = 12000
        if params.allow_address_scan == true or params.allow_address_scan == "true" then
            max_scan = 50000
        end
        local found = nil
        local scanned = 0
        pcall(function()
            ForEachUObject(function(obj, _chunk, _idx)
                if not obj then
                    return
                end
                scanned = scanned + 1
                if scanned > max_scan then
                    return true
                end
                local ok, addr = pcall(function()
                    return obj:GetAddress()
                end)
                if ok and addr == target then
                    found = obj
                    return true
                end
            end)
        end)
        if found then
            local ok_v, valid = pcall(function()
                return found:IsValid()
            end)
            if ok_v and valid then
                return found, nil
            end
        end
        return nil, "object not found for address (scanned " .. tostring(scanned) .. ")"
    end

    if params.class and params.class ~= "" then
        local ok, obj = pcall(function()
            return FindFirstOf(params.class)
        end)
        if ok and obj and obj:IsValid() then
            return obj, nil
        end
        return nil, "no instance of class " .. params.class
    end

    return nil, "provide full_name, address, or class"
end

local function list_by_class(class_name, limit, name_contains)
    limit = tonumber(limit) or 50
    if limit < 1 then
        limit = 1
    end
    if limit > 500 then
        limit = 500
    end
    name_contains = name_contains and string.lower(name_contains) or nil

    local results = {}
    local ok, objs = pcall(function()
        return FindAllOf(class_name)
    end)
    if not ok or objs == nil then
        return results
    end

    if type(objs) == "userdata" or (type(objs) == "table" and objs.IsValid) then
        -- single object
        local s = object_summary(objs)
        if s then
            results[#results + 1] = s
        end
        return results
    end

    for _, obj in pairs(objs) do
        if #results >= limit then
            break
        end
        local s = object_summary(obj)
        if s then
            if not name_contains or (s.full_name and string.find(string.lower(s.full_name), name_contains, 1, true))
                or (s.name and string.find(string.lower(s.name), name_contains, 1, true)) then
                results[#results + 1] = s
            end
        end
    end
    return results
end

-- Time-budgeted reflection walks: hard stop before hanging the game thread.
-- Per-struct budgets (not one budget for the whole hierarchy) so engine layers
-- are actually pageable instead of dying on the first Engine super.
local REFLECT_BUDGET_SEC = 0.030
local ENGINE_STRUCT_BUDGET_SEC = 0.020

local function budget_ok(t0, limit)
    limit = limit or REFLECT_BUDGET_SEC
    return (os.clock() - t0) < limit
end

local function new_job_id()
    return string.format("job_%d_%d", os.time(), work_count + poll_count)
end

local function is_engine_struct_name(full)
    if not full or type(full) ~= "string" then
        return false
    end
    -- Prefer game/BP props; deep Engine/CoreUObject walks are huge and crashy on BFBB.
    if string.find(full, "/Script/Engine.", 1, true) then
        return true
    end
    if string.find(full, "/Script/CoreUObject.", 1, true) then
        return true
    end
    return false
end

-- Skip pure CoreUObject UObject noise when listing engine (still allow Actor/Pawn/Character).
local function is_core_uobject_only(full)
    if not full or type(full) ~= "string" then
        return false
    end
    return string.find(full, "/Script/CoreUObject.Object", 1, true) ~= nil
        or string.find(full, "/Script/CoreUObject.Interface", 1, true) ~= nil
end

local function collect_properties(obj, limit, include_values, max_depth, offset, bp_only)
    limit = tonumber(limit) or 80
    include_values = include_values and true or false
    max_depth = tonumber(max_depth) or 4
    offset = tonumber(offset) or 0
    if offset < 0 then
        offset = 0
    end
    if bp_only == nil then
        bp_only = true -- default safer: Blueprint/game modules first
    end
    local props = {}
    local seen = 0
    local stopped = false
    local page_full = false
    local class = nil
    pcall(function()
        class = obj:GetClass()
    end)
    if not class then
        return props, 0, false
    end

    local function walk(struct, depth)
        if stopped or page_full or not struct or depth > max_depth then
            return
        end
        local struct_full = nil
        pcall(function()
            if struct.GetFullName then
                struct_full = struct:GetFullName()
            end
        end)
        -- Skip pure engine supers when bp_only (still list game/BP/Pineapple fields).
        if bp_only and depth > 0 and is_engine_struct_name(struct_full) then
            return
        end
        -- Engine listing: skip empty CoreUObject root noise; keep Engine.Actor etc.
        if not bp_only and depth > 0 and is_core_uobject_only(struct_full) then
            return
        end

        -- Fresh budget per struct so engine layers are readable via paging.
        local t_struct = os.clock()
        local struct_limit = (depth > 0 and is_engine_struct_name(struct_full))
            and ENGINE_STRUCT_BUDGET_SEC
            or REFLECT_BUDGET_SEC
        local struct_budget_hit = false

        pcall(function()
            if struct.ForEachProperty then
                struct:ForEachProperty(function(prop)
                    if page_full or #props >= limit then
                        page_full = true
                        stopped = true
                        return true
                    end
                    if not budget_ok(t_struct, struct_limit) then
                        struct_budget_hit = true
                        stopped = true -- more names exist; client should page
                        return true
                    end
                    local name, full = nil, nil
                    pcall(function()
                        name = prop:GetFName():ToString()
                    end)
                    -- Names-only: skip GetFullName on engine supers (cheaper / less Fatal-prone).
                    if include_values or (depth == 0 and not is_engine_struct_name(struct_full)) then
                        pcall(function()
                            full = prop:GetFullName()
                        end)
                    end
                    if seen < offset then
                        seen = seen + 1
                        return
                    end
                    seen = seen + 1
                    local entry = {
                        name = name,
                        full_name = full,
                        engine = is_engine_struct_name(struct_full) or nil,
                        depth = depth,
                    }
                    if include_values and name then
                        local lower = string.lower(name)
                        if not string.find(lower, "delegate", 1, true)
                            and not string.find(lower, "array", 1, true)
                            and not string.find(lower, "map", 1, true) then
                            local okv, value = pcall(function()
                                return serialize_value(obj:GetPropertyValue(name))
                            end)
                            if okv then
                                entry.value = value
                            end
                        end
                    end
                    props[#props + 1] = entry
                end)
            end
        end)

        if page_full or #props >= limit then
            stopped = true
            return
        end
        -- If this struct timed out mid-walk, stop hierarchy so offset can resume.
        if struct_budget_hit then
            stopped = true
            return
        end
        pcall(function()
            if struct.GetSuperStruct then
                walk(struct:GetSuperStruct(), depth + 1)
            end
        end)
    end

    walk(class, 0)
    return props, seen, stopped
end

local function collect_functions(obj, limit, max_depth, bp_only, offset)
    limit = tonumber(limit) or 40
    max_depth = tonumber(max_depth) or 2
    offset = tonumber(offset) or 0
    if offset < 0 then
        offset = 0
    end
    if bp_only == nil then
        bp_only = true
    end
    local funcs = {}
    local seen = 0
    local stopped = false
    local page_full = false
    local class = nil
    pcall(function()
        class = obj:GetClass()
    end)
    if not class then
        return funcs, false, 0
    end

    local function walk(struct, depth)
        if stopped or page_full or not struct or depth > max_depth then
            return
        end
        local struct_full = nil
        pcall(function()
            if struct.GetFullName then
                struct_full = struct:GetFullName()
            end
        end)
        if bp_only and depth > 0 and is_engine_struct_name(struct_full) then
            return
        end
        if not bp_only and depth > 0 and is_core_uobject_only(struct_full) then
            return
        end

        local t_struct = os.clock()
        local struct_limit = (depth > 0 and is_engine_struct_name(struct_full))
            and ENGINE_STRUCT_BUDGET_SEC
            or REFLECT_BUDGET_SEC
        local struct_budget_hit = false

        pcall(function()
            if struct.ForEachFunction then
                struct:ForEachFunction(function(fn)
                    if page_full or #funcs >= limit then
                        page_full = true
                        stopped = true
                        return true
                    end
                    if not budget_ok(t_struct, struct_limit) then
                        struct_budget_hit = true
                        stopped = true
                        return true
                    end
                    local name, full = nil, nil
                    pcall(function()
                        name = fn:GetFName():ToString()
                    end)
                    -- Skip GetFullName on engine supers (names only is enough for discovery)
                    if depth == 0 or not is_engine_struct_name(struct_full) then
                        pcall(function()
                            full = fn:GetFullName()
                        end)
                    end
                    if seen < offset then
                        seen = seen + 1
                        return
                    end
                    seen = seen + 1
                    funcs[#funcs + 1] = {
                        name = name,
                        full_name = full,
                        engine = is_engine_struct_name(struct_full) or nil,
                        depth = depth,
                    }
                end)
            end
        end)

        if page_full or #funcs >= limit then
            stopped = true
            return
        end
        if struct_budget_hit then
            stopped = true
            return
        end
        pcall(function()
            if struct.GetSuperStruct then
                walk(struct:GetSuperStruct(), depth + 1)
            end
        end)
    end

    walk(class, 0)
    return funcs, stopped, seen
end

-- Locate a UFunction by exact name. Returns found, full_name, note, fn_obj
-- Prefer GetFunction / FindFunctionByName when available; else bounded walk.
local function find_ufunction(obj, fname)
    local class = nil
    pcall(function()
        class = obj:GetClass()
    end)
    if not class then
        return false, nil, "no class", nil
    end

    local fast = nil
    pcall(function()
        if obj.GetFunction then
            fast = obj:GetFunction(fname)
        end
    end)
    if not fast then
        pcall(function()
            if class.FindFunctionByName then
                fast = class:FindFunctionByName(fname)
            end
        end)
    end
    if fast then
        local valid = true
        pcall(function()
            if fast.IsValid then
                valid = fast:IsValid()
            end
        end)
        if valid then
            local full = nil
            pcall(function()
                full = fast:GetFullName()
            end)
            return true, full, "fast_path", fast
        end
    end

    local t0 = os.clock()
    local found_full, found_fn = nil, nil
    local depth = 0
    local max_depth = 8
    local cur = class
    while cur and depth <= max_depth and budget_ok(t0) do
        local hit = false
        pcall(function()
            if cur.ForEachFunction then
                cur:ForEachFunction(function(fn)
                    if not budget_ok(t0) then
                        return true
                    end
                    local n = nil
                    pcall(function()
                        n = fn:GetFName():ToString()
                    end)
                    if n == fname then
                        found_fn = fn
                        pcall(function()
                            found_full = fn:GetFullName()
                        end)
                        hit = true
                        return true
                    end
                end)
            end
        end)
        if hit then
            return true, found_full, nil, found_fn
        end
        local next_s = nil
        pcall(function()
            if cur.GetSuperStruct then
                next_s = cur:GetSuperStruct()
            end
        end)
        cur = next_s
        depth = depth + 1
    end
    return false, nil, "not found within budgeted class walk", nil
end

-- Describe UFunction parameters from reflection (no ProcessEvent).
local function describe_ufunction_params(fn_obj)
    local params = {}
    if not fn_obj then
        return params
    end
    local t0 = os.clock()
    pcall(function()
        if fn_obj.ForEachProperty then
            fn_obj:ForEachProperty(function(prop)
                if not budget_ok(t0) or #params >= 40 then
                    return true
                end
                local name, full, flags = nil, nil, nil
                pcall(function()
                    name = prop:GetFName():ToString()
                end)
                pcall(function()
                    full = prop:GetFullName()
                end)
                pcall(function()
                    if prop.GetPropertyFlags then
                        flags = prop:GetPropertyFlags()
                    end
                end)
                params[#params + 1] = {
                    name = name,
                    full_name = full,
                    flags = flags,
                }
            end)
        end
    end)
    return params
end

local function is_dangerous_ufunction_name(fname, full)
    local n = string.lower(tostring(fname or ""))
    local f = string.lower(tostring(full or ""))
    local blob = n .. " " .. f

    -- 0-arg is NOT safe. Debug dumps / RPCs / lifecycle still FATAL via ProcessEvent.
    if string.find(blob, "debug", 1, true) then
        return "Debug* function (native dump/assert risk — use property reads instead)"
    end
    if string.find(n, "executeubergraph", 1, true) then
        return "ExecuteUbergraph (internal BP glue)"
    end
    if string.find(n, "__delegatesignature", 1, true) or string.find(n, "delegatesignature", 1, true) then
        return "delegate signature (not a normal call)"
    end
    if string.find(n, "receive", 1, true) and string.find(n, "tick", 1, true) then
        return "tick/receive path (engine life-cycle)"
    end
    if string.find(f, "latent", 1, true) or string.find(n, "latent", 1, true) then
        return "looks latent"
    end
    -- Network RPCs — wrong context crashes shipping
    if string.sub(n, 1, 6) == "server" then
        return "Server* RPC (not safe offline ProcessEvent)"
    end
    if string.sub(n, 1, 9) == "multicast" then
        return "Multicast* RPC"
    end
    if string.sub(n, 1, 6) == "client" and n ~= "client" then
        return "Client* RPC"
    end
    -- Destructive / lifecycle
    if string.find(n, "destroy", 1, true) or string.find(n, "k2_destroy", 1, true) then
        return "Destroy* (destructive)"
    end
    if n == "beginplay" or n == "endplay" or n == "tick" then
        return "lifecycle function"
    end
    return nil
end

-- Property exists check (names only) before get/set.
-- Phase 1: BP/game supers. Phase 2 (allow_engine): Engine supers one-by-one with
-- a short budget each. Never one long ForEach across the whole hierarchy (Fatal risk).
local function find_uproperty(obj, pname, allow_engine)
    local class = nil
    pcall(function()
        class = obj:GetClass()
    end)
    if not class then
        return false, nil
    end

    local function scan_struct(struct, use_engine_budget)
        if not struct then
            return false, nil
        end
        local t0 = os.clock()
        local lim = use_engine_budget and ENGINE_STRUCT_BUDGET_SEC or REFLECT_BUDGET_SEC
        local found_full = nil
        local hit = false
        pcall(function()
            if struct.ForEachProperty then
                struct:ForEachProperty(function(prop)
                    if not budget_ok(t0, lim) then
                        return true
                    end
                    local n = nil
                    pcall(function()
                        n = prop:GetFName():ToString()
                    end)
                    if n == pname then
                        -- Avoid GetFullName during engine scans (was Fatal-adjacent).
                        if not use_engine_budget then
                            pcall(function()
                                found_full = prop:GetFullName()
                            end)
                        end
                        hit = true
                        return true
                    end
                end)
            end
        end)
        return hit, found_full
    end

    local cur = class
    local depth = 0
    local max_depth = allow_engine and 10 or 5
    while cur and depth <= max_depth do
        local struct_full = nil
        pcall(function()
            if cur.GetFullName then
                struct_full = cur:GetFullName()
            end
        end)
        local is_eng = depth > 0 and is_engine_struct_name(struct_full)
        if is_eng and not allow_engine then
            break
        end
        if is_eng and is_core_uobject_only(struct_full) then
            break
        end
        local hit, found_full = scan_struct(cur, is_eng)
        if hit then
            return true, found_full
        end
        local next_s = nil
        pcall(function()
            if cur.GetSuperStruct then
                next_s = cur:GetSuperStruct()
            end
        end)
        cur = next_s
        depth = depth + 1
    end
    return false, nil
end

local function handle_command(cmd, params)
    params = params or {}
    cmd = tostring(cmd or "")

    if cmd == "ping" then
        return {
            ok = true,
            pong = true,
            protocol = PROTOCOL_VERSION,
            mod = MOD_NAME,
            uptime_sec = os.time() - started_at,
        }
    end

    if cmd == "status" or cmd == "capabilities" then
        local major, minor, hotfix = nil, nil, nil
        pcall(function()
            if UE4SS and UE4SS.GetVersion then
                major, minor, hotfix = UE4SS.GetVersion()
            end
        end)
        local ue_major, ue_minor = nil, nil
        pcall(function()
            if UnrealVersion then
                ue_major = UnrealVersion.GetMajor()
                ue_minor = UnrealVersion.GetMinor()
            end
        end)
        return {
            ok = true,
            protocol = PROTOCOL_VERSION,
            mod = MOD_NAME,
            runtime = "ue4ss-lua",
            ue4ss_version = { major = major, minor = minor, hotfix = hotfix },
            unreal_version = { major = ue_major, minor = ue_minor },
            ipc_dir = ipc_dir,
            uptime_sec = os.time() - started_at,
            offsets = "owned_by_ue4ss_signature_scan",
            commands = {
                "ping", "status", "capabilities",
                "list_actors", "find_objects", "search_objects",
                "get_object", "get_properties", "get_property", "set_property",
                "get_gear_loadout",
                "list_functions", "call_function", "has_function", "probe_function",
                "describe_function",
                "get_player", "execute_console",
                "for_each_sample", "sample_uobjects",
                "get_map_entry", "set_map_entry",
                "poll_job", "cancel_job",
            },
            protocol_major = 3,
            version = PROTOCOL_VERSION,
            semver = "2.0.0",
            pump = PUMP_ID,
            engine_reflection = {
                list = "get_properties/list_functions bp_only=false + offset paging",
                read = "get_property scans BP then bounded engine supers",
                write = "set_property BP-first; pass allow_engine=true for engine fields",
            },
            game_state_suspect = game_state_suspect,
            call_safety = {
                precheck_exists = true,
                describe_function = true,
                default_zero_args_only = true,
                refuse_missing = true,
                note = "3.3.0-beta.1: signature describe + refuse mismatched arity. Native asserts may still need FatalGuard. User launches game.",
            },
            heavy_tools = {
                sample_uobjects = "jobbed_chunked",
                list_functions = "time_budgeted",
                get_properties = "paged",
            },
        }
    end

    if cmd == "list_actors" then
        local limit = tonumber(params.limit) or 25
        if limit > 100 then
            limit = 100
        end
        local name_contains = params.name_contains or params.name or ""
        -- Prefer concrete subclasses first; full Actor scans can be huge mid-load.
        local class_name = params.class or "Pawn"
        local actors = list_by_class(class_name, limit, name_contains)
        if #actors == 0 then
            actors = list_by_class("Actor", math.min(limit, 25), name_contains)
            class_name = "Actor"
        end
        return { ok = true, count = #actors, class = class_name, objects = actors }
    end

    if cmd == "find_objects" then
        local class_name = params.class or params.class_name or ""
        if class_name == "" then
            return { ok = false, error = "class is required" }
        end
        local objs = list_by_class(class_name, params.limit or 50, params.name_contains or params.name)
        return { ok = true, class = class_name, count = #objs, objects = objs }
    end

    if cmd == "search_objects" then
        -- Prefer Pawn over Actor by default (Actor FindAllOf is huge on BFBB).
        local class_name = params.class or "Pawn"
        local query = params.query or params.name or ""
        if query == "" then
            return { ok = false, error = "query is required" }
        end
        local lim = tonumber(params.limit) or 30
        if lim > 80 then
            lim = 80
        end
        -- Searching all Actors is allowed but capped hard.
        if class_name == "Actor" and lim > 40 then
            lim = 40
        end
        local objs = list_by_class(class_name, lim, query)
        return { ok = true, query = query, class = class_name, count = #objs, objects = objs, safe = true }
    end

    if cmd == "get_object" then
        local obj, err = resolve_object(params)
        if not obj then
            return { ok = false, error = err or "not found" }
        end
        return { ok = true, object = object_summary(obj) }
    end

    if cmd == "get_properties" then
        local obj, err = resolve_object(params)
        if not obj then
            return { ok = false, error = err or "not found" }
        end
        local include_values = params.include_values == true or params.include_values == "true"
        local lim = tonumber(params.limit) or 60
        local offset = tonumber(params.offset) or 0
        local max_lim = include_values and 25 or 120
        if lim > max_lim then
            lim = max_lim
        end
        local depth = tonumber(params.max_depth) or 3
        if depth > 6 then
            depth = 6
        end
        local bp_only = params.bp_only ~= false and params.bp_only ~= "false"
        local props, walked, budget_hit = collect_properties(
            obj,
            lim,
            include_values,
            depth,
            offset,
            bp_only
        )
        return {
            ok = true,
            object = object_summary(obj),
            properties = props,
            count = #props,
            offset = offset,
            next_offset = offset + #props,
            walked_through = walked,
            include_values = include_values,
            truncated = (#props >= lim) or budget_hit,
            budget_hit = budget_hit,
            bp_only = bp_only,
            max_depth = depth,
            safe = true,
            hint = include_values and "values mode is limited; prefer get_property"
                or "names only (time-budgeted); pass offset for next page if truncated=true; bp_only=false for engine supers",
        }
    end

    if cmd == "get_property" then
        local obj, err = resolve_object(params)
        if not obj then
            return { ok = false, error = err or "not found" }
        end
        local name = params.property or params.member or params.name
        if not name or name == "" then
            return { ok = false, error = "property is required" }
        end
        local force = params.force == true or params.force == "true"
        -- Reads: engine allowed by default (bounded per-struct scan). Writes stay BP-first.
        local allow_engine = true
        if params.allow_engine == false or params.allow_engine == "false" then
            allow_engine = false
        end
        if force then
            allow_engine = true
        end

        -- BFBB / UE4.23: GetPropertyValue(Controller|PlayerState) on unpossessed pawns can
        -- HARD FREEZE the game thread (pcall cannot catch). Skip the native read when
        -- IsPlayerControlled is false — return NullObject unless force=true.
        local pname = tostring(name)
        if not force and (pname == "Controller" or pname == "PlayerState" or pname == "LastController") then
            local player_ctrl = nil
            pcall(function()
                if obj.IsPlayerControlled then
                    player_ctrl = obj:IsPlayerControlled()
                end
            end)
            if player_ctrl == false then
                return {
                    ok = true,
                    object = object_summary(obj),
                    property = name,
                    value = {
                        __type = "NullObject",
                        is_valid = false,
                        note = "skipped native read on unpossessed pawn (GetPropertyValue hangs on BFBB)",
                    },
                    safe_precheck = true,
                    skipped_hang_prone = true,
                    is_object_property = true,
                    value_kind = "NullObject",
                    protocol = PROTOCOL_VERSION,
                }
            end
        end

        local exists, pfull = find_uproperty(obj, name, allow_engine)
        if not exists and not force then
            return {
                ok = false,
                error = "property not found (BP + bounded engine scan): " .. tostring(name),
                property = name,
                object = object_summary(obj),
                safe_refuse = true,
                hint = "List names with get_properties (bp_only=false for engine). Avoid unknown names.",
            }
        end
        local ok, value = pcall(function()
            return obj:GetPropertyValue(name)
        end)
        if not ok then
            ok, value = pcall(function()
                return obj[name]
            end)
        end
        if not ok then
            return { ok = false, error = "failed to read property: " .. tostring(value) }
        end

        local pfull_s = tostring(pfull or "")
        local is_array_prop = string.find(pfull_s, "ArrayProperty", 1, true) ~= nil
        local is_object_prop = string.find(pfull_s, "ObjectProperty", 1, true) ~= nil
            or string.find(pfull_s, "WeakObjectProperty", 1, true) ~= nil
            or string.find(pfull_s, "SoftObjectProperty", 1, true) ~= nil
            or string.find(pfull_s, "ClassProperty", 1, true) ~= nil
            or string.find(pfull_s, "SoftClassProperty", 1, true) ~= nil

        -- Null object refs (unpossessed Pawn.Controller / PlayerState): never hang serialize.
        if value == nil then
            local null_val = nil
            if is_object_prop and not is_array_prop then
                null_val = {
                    __type = "NullObject",
                    is_valid = false,
                    note = "property is null (e.g. no Controller on unpossessed pawn)",
                }
            end
            return {
                ok = true,
                object = object_summary(obj),
                property = name,
                property_full = pfull,
                value = null_val,
                safe_precheck = true,
                allow_engine = allow_engine,
                is_array_property = is_array_prop,
                is_object_property = is_object_prop,
                value_kind = null_val and "NullObject" or "null",
                protocol = PROTOCOL_VERSION,
            }
        end

        -- Object refs: UObject path only (never array index probe)
        local serialized = nil
        local ser_ok, ser_err = true, nil
        if is_object_prop and not is_array_prop then
            ser_ok, serialized = pcall(function()
                -- Guard: invalid userdata before any method calls
                local iv_ok, iv = pcall(function()
                    if value.IsValid then
                        return value:IsValid()
                    end
                    return true
                end)
                if not iv_ok then
                    return {
                        __type = "InvalidObject",
                        is_valid = false,
                        note = "IsValid threw; treating as null ref",
                    }
                end
                if iv == false then
                    return {
                        __type = "InvalidObject",
                        is_valid = false,
                        note = "null/stale object property",
                    }
                end
                local uo = serialize_uobject(value)
                if uo then
                    return uo
                end
                return serialize_value(value)
            end)
            if not ser_ok then
                -- Never leave client hanging: soft-fail null-like
                serialized = {
                    __type = "InvalidObject",
                    is_valid = false,
                    note = "serialize failed: " .. tostring(serialized),
                }
                ser_ok = true
            end
        else
            ser_ok, serialized = pcall(function()
                return serialize_value(value)
            end)
        end
        if not ser_ok then
            return {
                ok = false,
                error = "read ok but serialize failed: " .. tostring(serialized),
                property = name,
                property_full = pfull,
                object = object_summary(obj),
                safe_precheck = true,
            }
        end

        return {
            ok = true,
            object = object_summary(obj),
            property = name,
            property_full = pfull,
            value = serialized,
            safe_precheck = true,
            allow_engine = allow_engine,
            is_array_property = is_array_prop,
            is_object_property = is_object_prop,
            value_kind = type(serialized) == "table" and serialized.__type or type(serialized),
            protocol = PROTOCOL_VERSION,
        }
    end

    -- Full goat gear snapshot: EquippedGear is incomplete; merge GearSlots structs too.
    if cmd == "get_gear_loadout" then
        local manager = nil
        local mgr_err = nil
        -- Prefer explicit object, else player pawn's GoatGearManager
        if params.full_name or params.address or params.class or params.class_name then
            manager, mgr_err = resolve_object(params)
        end
        if not manager then
            local controller = nil
            local pawn = nil
            pcall(function()
                if UEHelpers and UEHelpers.GetPlayerController then
                    controller = UEHelpers.GetPlayerController()
                end
            end)
            if not controller then
                pcall(function()
                    controller = FindFirstOf("PlayerController")
                end)
            end
            if controller then
                pcall(function()
                    pawn = controller.Pawn
                end)
            end
            if pawn then
                pcall(function()
                    manager = pawn:GetPropertyValue("GoatGearManager")
                end)
                if not manager then
                    pcall(function()
                        manager = pawn.GoatGearManager
                    end)
                end
            end
            if not manager then
                pcall(function()
                    manager = FindFirstOf("GGGoatGearManager")
                end)
            end
        end
        if not manager then
            return {
                ok = false,
                error = mgr_err or "GoatGearManager not found",
                safe = true,
            }
        end

        -- deep_struct / deep: opt-in only — light mode preferred so save data stays clean
        local deep = params.deep == true or params.deep == "true"
            or params.deep_struct == true or params.deep_struct == "true"
        local arr_mode = deep and "deep" or "light"

        local function read_arr(prop_name)
            local ok, raw = pcall(function()
                return manager:GetPropertyValue(prop_name)
            end)
            if not ok or raw == nil then
                return nil, "read_failed"
            end
            local ok_s, ser = pcall(function()
                return serialize_array(raw, 0, ARRAY_MAX_ITEMS, arr_mode)
            end)
            if ok_s and ser then
                return ser, "ok"
            end
            -- no generic serialize_value fallback on gear arrays (can ForEach+get dirty soft refs)
            return nil, "serialize_failed"
        end

        local equipped, eq_st = read_arr("EquippedGear")
        local slots, sl_st = read_arr("GearSlots")
        -- Skip unequipped/previous in light mode (extra soft-array traffic)
        local unequipped, un_st = nil, "skipped_light"
        local previous, pr_st = nil, "skipped_light"
        if deep then
            unequipped, un_st = read_arr("UnequippedGear")
            previous, pr_st = read_arr("PreviouslyEquippedGear")
        end

        local pieces = {}
        local function add_piece(source, item, index)
            if not is_useful_gear_ref(item) and not (type(item) == "table" and item.__type == "Struct") then
                return
            end
            -- Structs: pull nested gear class if present
            local summary = item
            if type(item) == "table" and item.__type == "Struct" and item.fields then
                local f = item.fields
                local gear = f.Gear or f.GearClass or f.EquippedClass or f.Class or f.SoftGearClass
                    or f.DataAsset or f.Item or f.ItemClass
                if gear and is_useful_gear_ref(gear) then
                    summary = {
                        __type = "GearPiece",
                        from = "struct_field",
                        gear = gear,
                        slot = f.Slot or f.SlotType or f.GearSlot or f.SlotId,
                        fields = f,
                    }
                else
                    summary = {
                        __type = "GearSlotStruct",
                        fields = f,
                        field_count = item.field_count,
                    }
                end
            end
            pieces[#pieces + 1] = {
                source = source,
                index = index,
                item = summary,
            }
        end

        if type(equipped) == "table" and equipped.items then
            for i, it in ipairs(equipped.items) do
                add_piece("EquippedGear", it, i)
            end
        end
        if type(slots) == "table" and slots.items then
            for i, it in ipairs(slots.items) do
                add_piece("GearSlots", it, i)
            end
        end

        return {
            ok = true,
            protocol = PROTOCOL_VERSION,
            manager = object_summary(manager),
            equipped_gear = equipped,
            gear_slots = slots,
            unequipped_gear = unequipped,
            previously_equipped = previous,
            status = {
                EquippedGear = eq_st,
                GearSlots = sl_st,
                UnequippedGear = un_st,
                PreviouslyEquippedGear = pr_st,
            },
            pieces = pieces,
            piece_count = #pieces,
            mode = arr_mode,
            safe = true,
            note = "light mode avoids ForEach/:get on soft arrays (save-side-effect risk). "
                .. "deep=true for aggressive struct probes. pieces merges EquippedGear + GearSlots.",
            save_warning = "If outfit fails to persist after MCP gear reads, re-equip once in-game before quit.",
        }
    end

    if cmd == "set_property" then
        local obj, err = resolve_object(params)
        if not obj then
            return { ok = false, error = err or "not found" }
        end
        local name = params.property or params.member or params.name
        if not name or name == "" then
            return { ok = false, error = "property is required" }
        end
        if params.value == nil and params.value_json == nil then
            return { ok = false, error = "value is required" }
        end
        local force = params.force == true or params.force == "true"
        -- Writes: BP/game first; engine only with allow_engine or force (safer).
        local allow_engine = params.allow_engine == true or params.allow_engine == "true" or force
        local exists, pfull = find_uproperty(obj, name, allow_engine)
        if not exists and not force then
            return {
                ok = false,
                error = "property not found for write (try allow_engine=true if engine field): " .. tostring(name),
                property = name,
                object = object_summary(obj),
                safe_refuse = true,
                hint = "List with get_properties first. Engine writes need allow_engine=true.",
            }
        end
        local value = params.value
        if params.value_json ~= nil and type(params.value_json) == "string" then
            local okj, decoded = pcall(function()
                return json.decode(params.value_json)
            end)
            if okj then
                value = decoded
            end
        end
        value = coerce_input(value)
        local ok, e = pcall(function()
            obj:SetPropertyValue(name, value)
        end)
        if not ok then
            ok, e = pcall(function()
                obj[name] = value
            end)
        end
        if not ok then
            return { ok = false, error = "failed to set property: " .. tostring(e) }
        end
        local new_val = nil
        pcall(function()
            new_val = serialize_value(obj:GetPropertyValue(name))
        end)
        return {
            ok = true,
            object = object_summary(obj),
            property = name,
            property_full = pfull,
            value = new_val,
            safe_precheck = true,
        }
    end

    if cmd == "list_functions" then
        local obj, err = resolve_object(params)
        if not obj then
            return { ok = false, error = err or "not found" }
        end
        local flim = tonumber(params.limit) or 30
        if flim > 80 then
            flim = 80
        end
        local offset = tonumber(params.offset) or 0
        if offset < 0 then
            offset = 0
        end
        local depth = tonumber(params.max_depth) or 2
        if depth > 6 then
            depth = 6
        end
        local bp_only = params.bp_only ~= false and params.bp_only ~= "false"
        local funcs, budget_hit, walked = collect_functions(obj, flim, depth, bp_only, offset)
        return {
            ok = true,
            object = object_summary(obj),
            functions = funcs,
            count = #funcs,
            offset = offset,
            next_offset = offset + #funcs,
            walked_through = walked,
            truncated = (#funcs >= flim) or budget_hit,
            budget_hit = budget_hit,
            bp_only = bp_only,
            max_depth = depth,
            safe = true,
            hint = "time-budgeted function list; page with offset if truncated=true",
        }
    end

    if cmd == "describe_function" then
        local obj, err = resolve_object(params)
        if not obj then
            return { ok = false, error = err or "not found" }
        end
        local fname = params.function_name or params.method or params.name
        if not fname or fname == "" then
            return { ok = false, error = "function_name is required" }
        end
        local found, found_full, search_note, fn_obj = find_ufunction(obj, fname)
        if not found then
            return {
                ok = false,
                error = "function not found: " .. tostring(fname),
                note = search_note,
                object = object_summary(obj),
            }
        end
        local sig = describe_ufunction_params(fn_obj)
        return {
            ok = true,
            exists = true,
            function_name = fname,
            full_name = found_full,
            parameters = sig,
            param_count = #sig,
            object = object_summary(obj),
            note = "reflection signature; use call_function with matching argc",
        }
    end

    if cmd == "call_function" or cmd == "has_function" or cmd == "probe_function" then
        local obj, err = resolve_object(params)
        if not obj then
            return { ok = false, error = err or "not found" }
        end
        local fname = params.function_name or params.method or params.name
        if not fname or fname == "" then
            return { ok = false, error = "function_name is required" }
        end
        local args = params.args or {}
        if type(args) ~= "table" then
            return { ok = false, error = "args must be an array" }
        end
        local dry_run = params.dry_run == true or params.dry_run == "true"
            or cmd == "has_function" or cmd == "probe_function"
        local allow_args = params.allow_args == true or params.allow_args == "true"
        local force = params.force == true or params.force == "true"

        local found, found_full, search_note, fn_obj = find_ufunction(obj, fname)
        if not found then
            return {
                ok = false,
                error = "function not found on object class (refused call — no ProcessEvent): " .. tostring(fname),
                function_name = fname,
                object = object_summary(obj),
                safe_refuse = true,
                note = search_note,
            }
        end

        local danger = is_dangerous_ufunction_name(fname, found_full)
        if danger and not force then
            return {
                ok = false,
                error = "refused call: " .. danger .. " (pass force=true only if you accept crash risk)",
                function_name = fname,
                full_name = found_full,
                object = object_summary(obj),
                safe_refuse = true,
            }
        end

        local sig = describe_ufunction_params(fn_obj)
        -- Count non-return-ish params when we can guess from names (ReturnValue).
        local expected_in = 0
        for _, p in ipairs(sig) do
            local n = string.lower(tostring(p.name or ""))
            if n ~= "returnvalue" and n ~= "return_value" then
                expected_in = expected_in + 1
            end
        end

        local argc = #args
        if argc > 0 and not allow_args and not force then
            return {
                ok = false,
                error = "refused call with arguments (use allow_args=true after describe_function).",
                function_name = fname,
                full_name = found_full,
                arg_count = argc,
                expected_in_params = expected_in,
                signature = sig,
                object = object_summary(obj),
                safe_refuse = true,
                exists = true,
            }
        end
        if #sig > 0 and not force and argc ~= expected_in and not dry_run then
            return {
                ok = false,
                error = string.format(
                    "arg count mismatch: got %d, signature suggests %d in-params (pass force=true to override)",
                    argc,
                    expected_in
                ),
                function_name = fname,
                full_name = found_full,
                arg_count = argc,
                expected_in_params = expected_in,
                signature = sig,
                object = object_summary(obj),
                safe_refuse = true,
                exists = true,
            }
        end

        if dry_run then
            return {
                ok = true,
                exists = true,
                dry_run = true,
                function_name = fname,
                full_name = found_full,
                object = object_summary(obj),
                would_call = true,
                arg_count = argc,
                expected_in_params = expected_in,
                signature = sig,
                note = "function exists; no ProcessEvent performed",
            }
        end

        local coerced = {}
        for i = 1, argc do
            coerced[i] = coerce_input(args[i])
        end

        -- 4) Call — pcall only catches Lua errors, NOT native asserts.
        --    Pre-checks above are the real safety net.
        local ok, result = pcall(function()
            return obj[fname](obj, table.unpack(coerced))
        end)
        if not ok then
            local ok2, result2 = pcall(function()
                local fn_obj = obj[fname]
                if obj.CallFunction and fn_obj then
                    return obj:CallFunction(fn_obj, table.unpack(coerced))
                end
                error(result)
            end)
            if not ok2 then
                return {
                    ok = false,
                    error = "call failed (Lua/pcall): " .. tostring(result),
                    function_name = fname,
                    full_name = found_full,
                    object = object_summary(obj),
                    note = "Native ProcessEvent asserts may still kill the process before pcall returns",
                }
            end
            return {
                ok = true,
                object = object_summary(obj),
                function_name = fname,
                full_name = found_full,
                result = serialize_value(result2),
                safe_precheck = true,
            }
        end
        return {
            ok = true,
            object = object_summary(obj),
            function_name = fname,
            full_name = found_full,
            result = serialize_value(result),
            safe_precheck = true,
        }
    end

    if cmd == "get_player" then
        local controller, pawn, camera = nil, nil, nil
        if UEHelpers then
            pcall(function()
                controller = UEHelpers.GetPlayerController and UEHelpers:GetPlayerController()
                    or (UEHelpers.GetPlayerController and UEHelpers.GetPlayerController())
            end)
        end
        if not controller or (controller.IsValid and not controller:IsValid()) then
            pcall(function()
                controller = FindFirstOf("PlayerController")
            end)
        end
        if controller and controller:IsValid() then
            pcall(function()
                pawn = controller.Pawn
            end)
            pcall(function()
                camera = controller.PlayerCameraManager
            end)
        end
        local loc = nil
        if pawn and pawn.IsValid and pawn:IsValid() then
            pcall(function()
                local v = pawn:K2_GetActorLocation()
                loc = serialize_value(v)
            end)
        end
        return {
            ok = true,
            controller = object_summary(controller),
            pawn = object_summary(pawn),
            camera_manager = object_summary(camera),
            pawn_location = loc,
        }
    end

    if cmd == "execute_console" or cmd == "console_command" then
        -- Run a player console command. Many PINE_* cheats need a live CheatManager
        -- (shipping builds leave CheatManager null). Construct it like UE4SS
        -- CheatManagerEnablerMod when missing.
        local command = params.command or params.text or params.console
        if not command or command == "" then
            return { ok = false, error = "command is required (e.g. PINE_SetShinyAmount 999999)" }
        end
        command = tostring(command)
        local controller = nil
        if UEHelpers then
            pcall(function()
                controller = UEHelpers.GetPlayerController and UEHelpers:GetPlayerController()
                    or (UEHelpers.GetPlayerController and UEHelpers.GetPlayerController())
            end)
        end
        if not controller or (controller.IsValid and not controller:IsValid()) then
            pcall(function()
                controller = FindFirstOf("PlayerController")
            end)
        end
        if not controller or (controller.IsValid and not controller:IsValid()) then
            return { ok = false, error = "no PlayerController for console command" }
        end

        local cheat_note = nil
        pcall(function()
            local cm = controller.CheatManager
            local valid = cm and cm.IsValid and cm:IsValid()
            if valid then
                cheat_note = "CheatManager already present"
                return
            end
            local cls = controller.CheatClass
            if not cls or (cls.IsValid and not cls:IsValid()) then
                cls = StaticFindObject("/Script/Engine.CheatManager")
            end
            if not cls or (cls.IsValid and not cls:IsValid()) then
                cheat_note = "no CheatClass / default CheatManager"
                return
            end
            local created = StaticConstructObject(cls, controller)
            if created and created.IsValid and created:IsValid() then
                controller.CheatManager = created
                cheat_note = "constructed CheatManager"
            else
                cheat_note = "failed to construct CheatManager"
            end
        end)

        local path_used = nil
        local ok, err = pcall(function()
            controller:ConsoleCommand(command, true)
            path_used = "PlayerController:ConsoleCommand(cmd,true)"
        end)
        if not ok then
            ok, err = pcall(function()
                controller:ConsoleCommand(command)
                path_used = "PlayerController:ConsoleCommand(cmd)"
            end)
        end
        if not ok then
            ok, err = pcall(function()
                local ksl = StaticFindObject("/Script/Engine.Default__KismetSystemLibrary")
                if not ksl then
                    error("no KismetSystemLibrary CDO")
                end
                local world = nil
                pcall(function()
                    world = controller:GetWorld()
                end)
                ksl:ExecuteConsoleCommand(world or controller, command, controller)
                path_used = "KismetSystemLibrary:ExecuteConsoleCommand"
            end)
        end
        -- Also try ProcessConsoleExec on controller / cheat manager (UE exec path).
        if ok then
            pcall(function()
                local cm = controller.CheatManager
                if cm and cm.IsValid and cm:IsValid() and cm.ProcessConsoleExec then
                    cm:ProcessConsoleExec(command, nil, controller)
                    path_used = (path_used or "") .. "+CheatManager:ProcessConsoleExec"
                end
            end)
        end
        if not ok then
            return {
                ok = false,
                error = "console command failed: " .. tostring(err),
                command = command,
                controller = object_summary(controller),
                cheat_note = cheat_note,
            }
        end
        local cm_after = nil
        pcall(function()
            local cm = controller.CheatManager
            if cm and cm.IsValid and cm:IsValid() then
                cm_after = object_summary(cm)
            end
        end)
        return {
            ok = true,
            command = command,
            path = path_used,
            controller = object_summary(controller),
            cheat_manager = cm_after,
            cheat_note = cheat_note,
            note = "Console command dispatched; verify with get_property on authoritative fields (not just HUD).",
        }
    end

    if cmd == "for_each_sample" or cmd == "sample_uobjects" then
        -- v3: jobbed chunked walk. First call starts job; poll_job continues.
        -- Small sync path when limit <= SAMPLE_CHUNK and no job_id.
        local limit = tonumber(params.limit) or 10
        if limit > 100 then
            limit = 100
        end
        local chunk = tonumber(params.chunk) or SAMPLE_CHUNK
        if chunk > 20 then
            chunk = 20
        end
        local name_contains = params.name_contains and string.lower(params.name_contains) or nil
        local class_contains = params.class_contains and string.lower(params.class_contains) or nil
        local job_id = params.job_id

        if job_id and jobs[job_id] then
            local job = jobs[job_id]
            if job.done then
                return {
                    ok = true,
                    job_id = job_id,
                    done = true,
                    count = #job.objects,
                    objects = job.objects,
                    partial = false,
                }
            end
            -- continue not used: sample jobs complete in start; keep for future
            return {
                ok = true,
                job_id = job_id,
                done = job.done,
                count = #job.objects,
                objects = job.objects,
                partial = not job.done,
            }
        end

        local out = {}
        local t0 = os.clock()
        local stopped = false
        pcall(function()
            ForEachUObject(function(obj, chunk_i, idx)
                if #out >= limit or not budget_ok(t0) then
                    stopped = true
                    return true
                end
                local ok_s, s = pcall(object_summary, obj)
                if not ok_s or not s then
                    return
                end
                if name_contains and not (s.full_name and string.find(string.lower(s.full_name), name_contains, 1, true)) then
                    return
                end
                if class_contains and not (s.class and string.find(string.lower(s.class), class_contains, 1, true)) then
                    return
                end
                s.chunk = chunk_i
                s.object_index = idx
                out[#out + 1] = s
            end)
        end)

        local jid = new_job_id()
        jobs[jid] = {
            kind = "sample_uobjects",
            objects = out,
            done = true,
            created = os.time(),
        }
        return {
            ok = true,
            job_id = jid,
            done = true,
            count = #out,
            objects = out,
            truncated = stopped or (#out >= limit),
            budget_hit = stopped,
            safe = true,
            note = "v3 chunked/budgeted GUObject sample; page with smaller limit if truncated",
        }
    end

    if cmd == "poll_job" then
        local job_id = params.job_id or params.id
        if not job_id or not jobs[job_id] then
            return { ok = false, error = "unknown job_id" }
        end
        local job = jobs[job_id]
        return {
            ok = true,
            job_id = job_id,
            done = job.done and true or false,
            count = job.objects and #job.objects or 0,
            objects = job.objects,
            result = job.result,
            partial = not job.done,
        }
    end

    if cmd == "cancel_job" then
        local job_id = params.job_id or params.id
        if job_id and jobs[job_id] then
            jobs[job_id] = nil
            return { ok = true, cancelled = job_id }
        end
        return { ok = false, error = "unknown job_id" }
    end

    if cmd == "get_map_entry" or cmd == "set_map_entry" then
        local obj, err = resolve_object(params)
        if not obj then
            return { ok = false, error = err or "not found" }
        end
        local map_name = params.map or params.property or params.map_name
        local key = params.key
        if not map_name or map_name == "" then
            return { ok = false, error = "map/property name is required" }
        end
        if key == nil or key == "" then
            return { ok = false, error = "key is required" }
        end
        local okm, map_val = pcall(function()
            return obj:GetPropertyValue(map_name)
        end)
        if not okm then
            return { ok = false, error = "failed to read map property: " .. tostring(map_val) }
        end

        -- UE4SS may expose TMap as table, userdata string, or custom.
        local as_table = nil
        if type(map_val) == "table" then
            as_table = map_val
        end

        if cmd == "get_map_entry" then
            if as_table then
                local v = as_table[key] or as_table[tostring(key)]
                return {
                    ok = true,
                    map = map_name,
                    key = key,
                    value = serialize_value(v),
                    object = object_summary(obj),
                    note = "table-backed map",
                }
            end
            return {
                ok = true,
                map = map_name,
                key = key,
                value = nil,
                raw = serialize_value(map_val),
                object = object_summary(obj),
                note = "TMap not fully table-exposed by UE4SS; raw shown. Use execute_console (PINE_SetShinyAmount) for BFBB currency when available.",
            }
        end

        -- set_map_entry
        local new_val = params.value
        if params.value_json ~= nil and type(params.value_json) == "string" then
            local okj, decoded = pcall(function()
                return json.decode(params.value_json)
            end)
            if okj then
                new_val = decoded
            end
        end
        if as_table then
            as_table[key] = new_val
            as_table[tostring(key)] = new_val
            local oks, e = pcall(function()
                obj:SetPropertyValue(map_name, as_table)
            end)
            if not oks then
                return { ok = false, error = "set map failed: " .. tostring(e) }
            end
            return {
                ok = true,
                map = map_name,
                key = key,
                value = serialize_value(new_val),
                object = object_summary(obj),
            }
        end
        return {
            ok = false,
            error = "cannot write TMap via reflection on this UE4SS build (not table-backed)",
            map = map_name,
            key = key,
            raw = serialize_value(map_val),
            hint = "try execute_console with game cheat e.g. PINE_SetShinyAmount 999999 after CheatManager construct",
        }
    end

    return { ok = false, error = "unknown command: " .. cmd }
end

local function write_response(id, body)
    body.id = id
    body.protocol = PROTOCOL_VERSION
    body.ts = os.time()
    local path = path_join(ipc_dir, "response.json")
    local flag = path_join(ipc_dir, "response.flag")
    delete_file(flag)
    write_file(path, json.encode(body))
    write_file(flag, "1")
end

local function write_heartbeat()
    last_hb_wall = os.time()
    local body = {
        ok = true,
        mod = MOD_NAME,
        protocol = PROTOCOL_VERSION,
        ts = os.time(),
        uptime_sec = os.time() - started_at,
        ipc_dir = ipc_dir,
        busy = busy,
        busy_cmd = last_cmd,
        last_error = last_error,
        poll_count = poll_count,
        work_count = work_count,
        ready = bridge_ready,
        gentle_boot = true,
        mcp_keepalive = true,
        pump = PUMP_ID,
        version = PROTOCOL_VERSION,
        game_state_suspect = game_state_suspect,
        warmup_ms = BOOT_WARMUP_MS,
        work_age_sec = os.time() - last_work_wall,
    }
    write_file(path_join(ipc_dir, "heartbeat.json"), json.encode(body))
end

local function process_request_file()
    if busy and busy_since > 0 and (os.time() - busy_since) >= BUSY_STUCK_SEC then
        log("busy watchdog: clearing stuck busy after " .. tostring(BUSY_STUCK_SEC) .. "s cmd=" .. tostring(last_cmd))
        -- Fail any in-flight request so the client unblocks.
        pcall(function()
            write_response(nil, {
                ok = false,
                error = "command watchdog timeout (MCP kept alive; previous cmd abandoned): " .. tostring(last_cmd),
                watchdog = true,
            })
        end)
        busy = false
        busy_since = 0
        last_error = "busy_watchdog_cleared"
    end
    if busy then
        return
    end
    local flag = path_join(ipc_dir, "request.flag")
    if not file_exists(flag) then
        return
    end

    local req_path = path_join(ipc_dir, "request.json")
    local raw = read_file(req_path)
    delete_file(flag)

    if not raw or raw == "" then
        write_response(nil, { ok = false, error = "empty request" })
        return
    end

    local ok_parse, req = pcall(function()
        return json.decode(raw)
    end)
    if not ok_parse or type(req) ~= "table" then
        write_response(nil, { ok = false, error = "invalid json request: " .. tostring(req) })
        return
    end

    local id = req.id
    local cmd = req.cmd or req.command
    local params = req.params or {}

    -- Always answer ping/status even during warmup or after fatal recovery.
    local c = tostring(cmd or "")
    local is_health = (c == "ping" or c == "status" or c == "capabilities")

    if not bridge_ready and not is_health then
        write_response(id, {
            ok = false,
            error = "bridge warming up (gentle boot); try again in a few seconds",
            ready = false,
            uptime_sec = os.time() - started_at,
        })
        return
    end

    busy = true
    busy_since = os.time()
    last_cmd = c
    log("cmd=" .. c)

    local function finish(result)
        if type(result) ~= "table" then
            result = { ok = true, result = serialize_value(result) }
        end
        write_response(id, result)
        busy = false
        busy_since = 0
        last_cmd = nil
        last_work_wall = os.time()
    end

    local ok, result = pcall(function()
        return handle_command(cmd, params)
    end)
    if not ok then
        last_error = tostring(result)
        log("handler error: " .. last_error)
        finish({ ok = false, error = "handler crash: " .. last_error })
        return
    end
    finish(result)
end

-- ---- RECOVERABLE PUMP (3.3.0-beta.1) ----
-- Goals after Fatal / hang / map load:
--   1) Clear stuck busy so new requests can run
--   2) Keep heartbeat moving (hook and/or delay)
--   3) Ctrl+F9 always recovers (cooldown so spam does not kill delay gens)
--   4) LoadMapPost re-arms delay chain (hooks alone may not re-fire delays)
--
-- Dual ExecuteWithDelay chains still forbidden (UE4SS #1180). One delay gen only.

local last_pump_clock = 0
local hooks_registered = false
local last_f9_time = 0
local F9_COOLDOWN_SEC = 1.0
local STALE_SEC = 2
local TICK_MIN_SEC = (TICK_MS or 250) / 1000.0
local boot_ready_forced = false

local function clear_busy(reason)
    if busy then
        log("clear_busy (" .. tostring(reason) .. ") was cmd=" .. tostring(last_cmd))
    end
    busy = false
    busy_since = 0
    last_cmd = nil
end

-- Forward decls (pump_once calls these; defined after arm_pump)
local force_revive
local consume_revive_flag

local function pump_once(reason)
    local nowc = os.clock()
    if (nowc - last_pump_clock) < TICK_MIN_SEC then
        return
    end
    last_pump_clock = nowc

    -- Agent/client can drop revive.flag into IPC to force re-arm without a keypress
    -- (only works if *some* hook/delay still runs once; else Ctrl+F9 / Ctrl+F10).
    pcall(consume_revive_flag)

    -- Wall-clock ready if warmup delay was GC'd
    if not bridge_ready and not boot_ready_forced then
        if (os.time() - started_at) >= math.floor(BOOT_WARMUP_MS / 1000) then
            bridge_ready = true
            boot_ready_forced = true
            log("bridge READY (wall-clock fallback)")
        end
    end

    poll_count = poll_count + 1
    work_count = work_count + 1
    last_work_wall = os.time()
    last_hb_wall = os.time()

    pcall(write_heartbeat)

    if busy and busy_since > 0 and (os.time() - busy_since) >= BUSY_STUCK_SEC then
        log("tick watchdog: abandon cmd=" .. tostring(last_cmd))
        pcall(function()
            write_response(nil, {
                ok = false,
                error = "watchdog abandoned stuck command: " .. tostring(last_cmd),
                watchdog = true,
                mcp_alive = true,
            })
        end)
        clear_busy("watchdog")
        last_error = "busy_watchdog_cleared"
        game_state_suspect = true
    end

    if not busy then
        local ok, err = pcall(process_request_file)
        if not ok then
            last_error = tostring(err)
            clear_busy("handler_error")
            log("tick work error: " .. last_error)
            game_state_suspect = true
        end
    end

    local now = os.time()
    for id, job in pairs(jobs) do
        if job.created and (now - job.created) > 300 then
            jobs[id] = nil
        end
    end
end

-- Single delay chain. force=true cancels prior gen and starts fresh (recovery).
local function arm_delay_chain(reason, force)
    local stale = (os.time() - last_work_wall) >= STALE_SEC
    if not force and not stale and pump_gen > 0 then
        -- Chain assumed alive; hooks may still be pumping.
        return
    end
    pump_gen = pump_gen + 1
    local my = pump_gen
    log("arm_delay gen=" .. tostring(my) .. " force=" .. tostring(force) .. " reason=" .. tostring(reason))
    local function step()
        if my ~= pump_gen then
            return
        end
        pcall(function()
            ExecuteWithDelay(TICK_MS, function()
                if my ~= pump_gen then
                    return
                end
                pcall(pump_once, "delay")
                step()
            end)
        end)
    end
    step()
end

local function has_no_hooks_marker()
    -- Present in game-profiles that crash on Lua RegisterHook (e.g. GS3).
    local candidates = {
        "Mods/UnrealEngineMCP/no_hooks.txt",
        "ue4ss/Mods/UnrealEngineMCP/no_hooks.txt",
        "UnrealEngineMCP/no_hooks.txt",
    }
    if ipc_dir then
        -- Win64/UnrealEngineMCP_IPC -> Win64
        local win64 = ipc_dir:gsub("[/\\]UnrealEngineMCP_IPC[/\\]?$", "")
        table.insert(candidates, path_join(win64, "ue4ss\\Mods\\UnrealEngineMCP\\no_hooks.txt"))
        table.insert(candidates, path_join(win64, "Mods\\UnrealEngineMCP\\no_hooks.txt"))
    end
    for _, p in ipairs(candidates) do
        local f = io.open(p, "r")
        if f then
            f:close()
            return true
        end
    end
    return false
end

local function register_tick_hooks(reason)
    if hooks_registered then
        return
    end
    hooks_registered = true
    -- NEVER hook Actor:ReceiveTick / Actor:Tick — fires for every actor every frame
    -- and has crashed shipping titles (e.g. Goat Simulator 3).
    -- Prefer PC ticks; fall back to viewport/camera/HUD (BFBB PC hooks often fail).
    if has_no_hooks_marker() then
        log("skip Lua RegisterHook (no_hooks.txt) reason=" .. tostring(reason))
        return
    end
    local paths = {
        "/Script/Engine.PlayerController:PlayerTick",
        "/Script/Engine.PlayerController:Tick",
        "/Script/Engine.GameViewportClient:Tick",
        "/Script/Engine.PlayerCameraManager:UpdateCamera",
        "/Script/Engine.HUD:ReceiveDrawHUD",
        "/Script/Engine.GameEngine:Tick",
    }
    local any = false
    for _, path in ipairs(paths) do
        local ok = pcall(function()
            RegisterHook(path, function()
                pcall(pump_once, "hook:" .. path)
            end)
        end)
        if ok then
            any = true
            log("RegisterHook ok: " .. path .. " (" .. tostring(reason) .. ")")
        else
            log("RegisterHook failed: " .. path .. " (" .. tostring(reason) .. ")")
        end
    end
    if not any then
        log("RegisterHook: no tick path bound (" .. tostring(reason) .. ") — delay + native kick only")
    end
end

-- Unified arm: hooks (once) + delay chain if stale or forced.
local function arm_pump(reason, force)
    force = force == true
    pcall(register_tick_hooks, reason)
    arm_delay_chain(reason, force)
    -- Prefer game-thread re-arm when available (post-Fatal delay gens can stick)
    pcall(function()
        if type(ExecuteInGameThread) == "function" then
            ExecuteInGameThread(function()
                pcall(arm_delay_chain, reason .. "-igt", true)
            end)
        end
    end)
    pcall(write_heartbeat)
end

--- Full revive after soft Fatal / hung pump (game may still be running).
--- Always runs one immediate pump cycle: RegisterKeyBind (and FatalGuard
--- synthetic Ctrl+F9) survive when ExecuteWithDelay does not.
---
--- Do NOT delete request.flag here — auto-kick can race MCP and wipe in-flight
--- work. Only clear busy + revive.flag; process_request_file runs immediately.
local last_hook_rebind = 0
force_revive = function(reason)
    reason = tostring(reason or "force_revive")
    clear_busy(reason)
    last_error = "revived:" .. reason
    bridge_ready = true
    game_state_suspect = true
    boot_ready_forced = true
    -- Re-bind hooks at most once per 30s (auto-kick every few seconds was
    -- stacking HUD hooks and never leaving a stable tick).
    local now = os.time()
    if (now - last_hook_rebind) >= 30 then
        hooks_registered = false
        last_hook_rebind = now
    end
    last_work_wall = os.time()
    last_hb_wall = os.time()
    last_pump_clock = 0
    log("FORCE REVIVE (" .. reason .. ") delay re-arm + immediate pump")
    pcall(function()
        if ipc_dir then
            -- Only drop revive.flag (consumed). Keep request.* for immediate pump.
            pcall(function()
                os.remove(path_join(ipc_dir, "revive.flag"))
            end)
        end
    end)
    pcall(write_heartbeat)
    arm_pump(reason, true)
    pcall(write_heartbeat)
    -- Critical: keybind/native-kick path must do work even if delays are dead.
    pcall(pump_once, "force_revive_immediate")
    pcall(write_heartbeat)
end

consume_revive_flag = function()
    if not ipc_dir then
        return false
    end
    local path = path_join(ipc_dir, "revive.flag")
    local f = io.open(path, "r")
    if not f then
        return false
    end
    f:close()
    pcall(function()
        os.remove(path)
    end)
    force_revive("revive.flag")
    return true
end

local function mark_ready(reason)
    bridge_ready = true
    clear_busy("mark_ready")
    last_work_wall = os.time()
    last_hb_wall = os.time()
    log("bridge READY (" .. tostring(reason) .. ")")
    pcall(write_heartbeat)
    arm_pump("ready-" .. tostring(reason), true)
end

local function schedule_ready(ms, reason)
    pcall(function()
        ExecuteWithDelay(ms, function()
            mark_ready(reason)
        end)
    end)
end

-- ---- boot ----
ipc_dir = detect_ipc_dir()
ensure_dir(ipc_dir)
bridge_ready = false
write_heartbeat()
log("bridge loaded " .. PROTOCOL_VERSION .. " pump=" .. PUMP_ID .. "; IPC dir = " .. tostring(ipc_dir))
log("hooks + single delay; Ctrl+F9 force re-arm; LoadMapPost re-arms")

arm_pump("boot", true)
schedule_ready(BOOT_WARMUP_MS, "boot-warmup")

pcall(function()
    RegisterLoadMapPostHook(function()
        log("LoadMapPost: recover pump")
        clear_busy("LoadMapPost")
        game_state_suspect = false
        -- Force delay re-arm — map loads often kill ExecuteWithDelay chains.
        arm_pump("LoadMapPost", true)
        schedule_ready(POST_MAP_WARMUP_MS, "LoadMapPost-delayed")
    end)
end)

local function bind_revive_key(key, label)
    pcall(function()
        RegisterKeyBind(key, { ModifierKey.CONTROL }, function()
            local now = os.clock()
            if (now - last_f9_time) < F9_COOLDOWN_SEC then
                clear_busy(label .. "-spam")
                bridge_ready = true
                pcall(write_heartbeat)
                return
            end
            last_f9_time = now
            force_revive(label)
        end)
    end)
end

-- Multiple revive keys (game may steal one; Fatal dialog focus is fine once dismissed)
bind_revive_key(Key.F9, "Ctrl+F9")
bind_revive_key(Key.F10, "Ctrl+F10")
bind_revive_key(Key.F8, "Ctrl+F8")

log("MCP recoverable pump armed — revive: IPC revive.flag (+ FatalGuard auto-kick), or Ctrl+F9/F10/F8")
