
#!/usr/bin/env python3
import pathlib, re

def gen():
    h = pathlib.Path("lora_proto.h").read_text(encoding="utf-8", errors="ignore")
    import re
    out = []
    for m in re.finditer(r'static\s+const\s+uint8_t\s+(DIR_\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*;', h):
        name, val = m.group(1), m.group(2)
        ival = int(val, 16) if val.startswith("0x") else int(val)
        out.append(f"{name} = {ival}")
    m = re.search(r'enum\s+Opcode7\s*:\s*uint8_t\s*\{(.*?)\};', h, flags=re.S)
    if m:
        block = m.group(1)
        for nm, val in re.findall(r'(OPC_[A-Z0-9_]+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)', block):
            ival = int(val, 16) if val.startswith("0x") else int(val)
            out.append(f"{nm} = {ival}")
    out.append("def make_type(direction:int, opcode:int) -> int: return (direction | (opcode & 0x7F))")
    out.append("def type_dir(t:int) -> int: return (t & 0x80)")
    out.append("def type_base(t:int) -> int: return (t & 0x7F)")
    code = "# Auto-generated from lora_proto.h\n" + "\n".join(out) + "\n"
    pathlib.Path("lora_proto_auto.py").write_text(code, encoding="utf-8")
    print("Wrote lora_proto_auto.py with", len(out)-3, "constants.")
if __name__ == "__main__":
    gen()
