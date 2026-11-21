# const.py
DOMAIN = "petkit_k3_local"
PLATFORMS = ["light", "button"]

CONF_DEVICES = "devices"
CONF_IDLE_TIMEOUT = "idle_timeout"

# Bluetooth-константы
CHAR_UUID = "0000aaa2-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000aaa1-0000-1000-8000-00805f9b34fb"
INIT_CMD = "fafcfdd501000000fb"

# Команды аутентификации – вставляем секрет между префиксом и суффиксом
AUTH_CMD_PREFIX = "fafcfd56010108000000"
AUTH_CMD_SUFFIX = "fb"

# Команды для управления
SPRAY_CMD = "fafcfddc010a02000103fb"
LIGHT_CMD = "fafcfddc010b02000203fb"

RESPONSE_OK = "00"
RESPONSE_DELAY = 0.5  # пауза между записью команды и чтением ответа, сек

# Энергосбережение: соединение открывается только для команды
# и закрывается после простоя (0 — сразу после команды)
DEFAULT_IDLE_TIMEOUT = 30
MAX_IDLE_TIMEOUT = 600
CONNECT_MAX_ATTEMPTS = 3

LIGHT_ON_DURATION = 10  # подсветка сама гаснет через 10 секунд