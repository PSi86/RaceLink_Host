# Auto-generated from lora_proto.h
DIR_M2N = 0
DIR_N2M = 128
OPC_DEVICES = 1
OPC_SET_GROUP = 2
OPC_WLED_CONTROL = 3
OPC_STATUS = 4
OPC_ACK = 126
def make_type(direction:int, opcode:int) -> int: return (direction | (opcode & 0x7F))
def type_dir(t:int) -> int: return (t & 0x80)
def type_base(t:int) -> int: return (t & 0x7F)
