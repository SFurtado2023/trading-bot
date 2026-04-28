from binance.client import Client
from binance.exceptions import BinanceAPIException
import pandas as pd
import pandas_ta as ta
import requests
import time
from datetime import datetime
import config

MONTO_POR_OPERACION = 5.0

def enviar_telegram(mensaje):
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": config.TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print(f"❌ Error Telegram: {e}")

def conectar():
    try:
        client = Client(config.API_KEY, config.API_SECRET)
        client.ping()
        print("✅ Conectado a Binance")
        enviar_telegram("✅ <b>Bot de Trading Activado</b>\nConectado a Binance y listo para operar.")
        return client
    except Exception as e:
        print(f"❌ Error: {e}")
        return None

def obtener_balance_usdt(client):
    cuenta = client.get_account()
    for activo in cuenta['balances']:
        if activo['asset'] == 'USDT':
            return float(activo['free'])
    return 0.0

def obtener_velas(client, par):
    try:
        raw = client.get_klines(
            symbol=par,
            interval=config.INTERVALO,
            limit=config.VELAS_HISTORIAL
        )
        df = pd.DataFrame(raw, columns=[
            'timestamp','open','high','low','close','volume',
            'close_time','quote_volume','trades',
            'taker_buy_base','taker_buy_quote','ignore'
        ])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        for col in ['open','high','low','close','volume']:
            df[col] = df[col].astype(float)
        df = df.set_index('timestamp')
        return df[['open','high','low','close','volume']]
    except Exception as e:
        print(f"❌ Error velas {par}: {e}")
        return None

def calcular_senales(client, par):
    df = obtener_velas(client, par)
    if df is None:
        return None

    df.ta.rsi(length=config.RSI_PERIODO, append=True)
    df.ta.macd(fast=config.MACD_RAPIDO, slow=config.MACD_LENTO,
               signal=config.MACD_SENAL, append=True)
    bb = df.ta.bbands(length=config.BB_PERIODO, std=config.BB_DESVIACION)
    df = pd.concat([df, bb], axis=1).dropna()

    ultima = df.iloc[-1]
    precio  = ultima['close']
    rsi     = ultima[f'RSI_{config.RSI_PERIODO}']
    macd    = ultima[f'MACD_{config.MACD_RAPIDO}_{config.MACD_LENTO}_{config.MACD_SENAL}']
    macd_s  = ultima[f'MACDs_{config.MACD_RAPIDO}_{config.MACD_LENTO}_{config.MACD_SENAL}']
    bb_u    = [c for c in df.columns if c.startswith('BBU_')]
    bb_l    = [c for c in df.columns if c.startswith('BBL_')]
    bb_upper = ultima[bb_u[0]] if bb_u else None
    bb_lower = ultima[bb_l[0]] if bb_l else None

    compra, venta = [], []

    if rsi < config.RSI_SOBREVENTA:
        compra.append(f"RSI={rsi:.1f} SOBREVENTA")
    elif rsi > config.RSI_SOBRECOMPRA:
        venta.append(f"RSI={rsi:.1f} SOBRECOMPRA")

    if macd > macd_s:
        compra.append("MACD alcista")
    else:
        venta.append("MACD bajista")

    if bb_lower and precio < bb_lower:
        compra.append("Precio bajo Bollinger")
    elif bb_upper and precio > bb_upper:
        venta.append("Precio sobre Bollinger")

    if len(compra) >= config.SENALES_MINIMAS_COMPRA:
        decision = "COMPRAR"
    elif len(venta) >= config.SENALES_MINIMAS_VENTA:
        decision = "VENDER"
    else:
        decision = "ESPERAR"

    return {
        "par": par, "decision": decision, "precio": precio,
        "rsi": rsi, "macd": macd, "compra": compra, "venta": venta
    }

def ejecutar_compra(client, par, precio):
    try:
        usdt = obtener_balance_usdt(client)
        if usdt < MONTO_POR_OPERACION:
            msg = f"⚠️ Sin USDT suficiente para comprar {par}\nDisponible: ${usdt:.2f}"
            print(f"   {msg}")
            enviar_telegram(msg)
            return False

        cantidad = MONTO_POR_OPERACION / precio
        info = client.get_symbol_info(par)
        step = float([f['stepSize'] for f in info['filters']
                      if f['filterType'] == 'LOT_SIZE'][0])
        decimales = len(str(step).rstrip('0').split('.')[-1])
        cantidad = round(cantidad - (cantidad % step), decimales)

        print(f"   🛒 Comprando {cantidad} {par} a ${precio:,.2f}")

        orden = client.order_market_buy(symbol=par, quantity=cantidad)

        msg = (f"🟢 <b>COMPRA EJECUTADA</b>\n"
               f"Par: {par}\n"
               f"Cantidad: {cantidad}\n"
               f"Precio: ${precio:,.2f}\n"
               f"Gastado: ${MONTO_POR_OPERACION:.2f} USDT\n"
               f"ID Orden: {orden['orderId']}")
        print(f"   ✅ COMPRA EJECUTADA — ID: {orden['orderId']}")
        enviar_telegram(msg)
        return True

    except BinanceAPIException as e:
        msg = f"❌ Error comprando {par}: {e}"
        print(f"   {msg}")
        enviar_telegram(msg)
        return False

def ejecutar_venta(client, par, precio_compra=None):
    try:
        moneda = par.replace('USDT', '')
        cuenta = client.get_account()
        cantidad = 0.0
        for activo in cuenta['balances']:
            if activo['asset'] == moneda:
                cantidad = float(activo['free'])
                break

        if cantidad <= 0:
            print(f"   ⚠️ Sin {moneda} para vender")
            return False

        info = client.get_symbol_info(par)
        step = float([f['stepSize'] for f in info['filters']
                      if f['filterType'] == 'LOT_SIZE'][0])
        decimales = len(str(step).rstrip('0').split('.')[-1])
        cantidad = round(cantidad - (cantidad % step), decimales)

        ticker = client.get_symbol_ticker(symbol=par)
        precio_actual = float(ticker['price'])
        valor_venta = cantidad * precio_actual

        orden = client.order_market_sell(symbol=par, quantity=cantidad)

        ganancia = ""
        if precio_compra:
            diff = ((precio_actual - precio_compra) / precio_compra) * 100
            ganancia = f"\nResultado: {'📈 +' if diff > 0 else '📉 '}{diff:.2f}%"

        msg = (f"🔴 <b>VENTA EJECUTADA</b>\n"
               f"Par: {par}\n"
               f"Cantidad: {cantidad}\n"
               f"Precio: ${precio_actual:,.2f}\n"
               f"Valor: ${valor_venta:.2f} USDT"
               f"{ganancia}\n"
               f"ID Orden: {orden['orderId']}")
        print(f"   ✅ VENTA EJECUTADA — ID: {orden['orderId']}")
        enviar_telegram(msg)
        return True

    except BinanceAPIException as e:
        msg = f"❌ Error vendiendo {par}: {e}"
        print(f"   {msg}")
        enviar_telegram(msg)
        return False

def ciclo_bot(client, numero_ciclo):
    ahora = datetime.now().strftime("%H:%M:%S")
    print(f"\n{'='*52}")
    print(f"  🤖 CICLO #{numero_ciclo} — {ahora}")
    print(f"{'='*52}")

    usdt = obtener_balance_usdt(client)
    print(f"  💵 USDT disponible: ${usdt:.2f}")

    resumen = []
    for par in config.PARES:
        resultado = calcular_senales(client, par)
        if resultado is None:
            continue

        decision = resultado['decision']
        precio   = resultado['precio']
        rsi      = resultado['rsi']
        macd     = resultado['macd']

        iconos = {"COMPRAR": "🟢", "VENDER": "🔴", "ESPERAR": "⏸️"}
        icon = iconos.get(decision, "⏸️")

        print(f"\n{icon}  {par} — ${precio:,.2f}")
        print(f"   RSI: {rsi:.1f}  |  MACD: {macd:.2f}")
        if resultado['compra']:
            print(f"   ✅ Señales compra: {resultado['compra']}")
        if resultado['venta']:
            print(f"   ⚠️  Señales venta : {resultado['venta']}")
        print(f"   ➤ Decisión: {decision}")

        resumen.append(f"{icon} {par}: ${precio:,.2f} | RSI:{rsi:.0f} → {decision}")

        if decision == "COMPRAR":
            ejecutar_compra(client, par, precio)
        elif decision == "VENDER":
            ejecutar_venta(client, par)

    # Resumen por Telegram cada ciclo
    msg_resumen = (f"🤖 <b>Ciclo #{numero_ciclo} — {ahora}</b>\n"
                   f"💵 USDT: ${usdt:.2f}\n\n" +
                   "\n".join(resumen))
    enviar_telegram(msg_resumen)

# ── ARRANQUE ──────────────────────────────────────────────
print("=" * 52)
print("  BOT DE TRADING — ETAPA 4")
print("  Con notificaciones Telegram")
print("  Monto por operación: $5 USDT")
print("  Presioná Ctrl+C para detener")
print("=" * 52)

client = conectar()

if client:
    ciclo = 1
    while True:
        ciclo_bot(client, ciclo)
        print(f"\n⏰ Próximo ciclo en 1 hora...")
        print("   (Presioná Ctrl+C para detener)")
        time.sleep(3600)
        ciclo += 1