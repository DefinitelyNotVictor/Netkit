#!/usr/bin/env python3
"""
Diagnóstico Rápido de Conexão - Ferramenta N2
Roda ping, traceroute e checagem de portas de gerência num CPE/roteador de cliente,
e sugere uma hipótese inicial de problema com base nos resultados.

Uso:
    python3 diagnostico.py <host>
    python3 diagnostico.py 192.168.0.1 --pings 20
    python3 diagnostico.py exemplo.com.br --ipv6
    python3 diagnostico.py 192.168.0.1 --watch 5
    python3 diagnostico.py 192.168.0.1 --portas 1723,53,8080:MeuServico
    python3 diagnostico.py 192.168.0.1 --no-color
"""

import socket
import subprocess
import argparse
import platform
import re
import statistics
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor


def obter_encoding_console():
    """
    No Windows, o console usa a página de código OEM (ex: cp850, cp860),
    diferente do encoding padrão do Python. Detecta dinamicamente para
    evitar caracteres acentuados corrompidos na saída do ping/tracert.
    """
    if platform.system().lower() == "windows":
        import ctypes
        codepage = ctypes.windll.kernel32.GetOEMCP()
        return f"cp{codepage}"
    return "utf-8"


ENCODING_CONSOLE = obter_encoding_console()


def decodificar_saida(saida_bytes):
    """Decodifica bytes da saída do comando usando o encoding correto do console."""
    if saida_bytes is None:
        return ""
    return saida_bytes.decode(ENCODING_CONSOLE, errors="replace")

def resolver_host(host, forcar_ipv6=False):
    """
    Resolve o host para IPv4 ou IPv6.
    Se forcar_ipv6=True, exige um endereço AAAA (erro se não existir).
    Caso contrário, tenta IPv4 primeiro e cai para IPv6 se não houver A record.
    Retorna (ip, versao) onde versao é 4 ou 6.
    """
    familia_desejada = socket.AF_INET6 if forcar_ipv6 else socket.AF_UNSPEC

    try:
        registros = socket.getaddrinfo(host, None, family=familia_desejada, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None, None

    if forcar_ipv6:
        for familia, _, _, _, endereco in registros:
            if familia == socket.AF_INET6:
                return endereco[0], 6
        return None, None

    # Sem forçar: prioriza IPv4, cai para IPv6 se só houver isso
    for familia, _, _, _, endereco in registros:
        if familia == socket.AF_INET:
            return endereco[0], 4
    for familia, _, _, _, endereco in registros:
        if familia == socket.AF_INET6:
            return endereco[0], 6

    return None, None


def resolver_nome_reverso(ip):
    """Tenta obter o hostname (DNS reverso) de um IP. Retorna None se não houver registro."""
    try:
        nome, _, _ = socket.gethostbyaddr(ip)
        return nome
    except (socket.herror, socket.gaierror, OSError):
        return None


# Portas de gerência mais comuns em CPEs/roteadores de ISP
PORTAS_GERENCIA = {
    23: "Telnet",
    80: "HTTP (admin web)",
    443: "HTTPS (admin web)",
    7547: "TR-069 (CWMP)",
    161: "SNMP",
}


# ---------- CORES ----------

CORES = {
    "reset": "\033[0m",
    "negrito": "\033[1m",
    "vermelho": "\033[91m",
    "amarelo": "\033[93m",
    "verde": "\033[92m",
    "azul": "\033[94m",
    "cinza": "\033[90m",
}

# Controlado pelo main() a partir do --no-color; módulo assume cor ativa por padrão
CORES_ATIVAS = True


def habilitar_ansi_windows():
    """
    No Windows, o suporte a códigos ANSI no cmd/PowerShell precisa ser
    habilitado explicitamente via SetConsoleMode. Se falhar (versões muito
    antigas do Windows), a chamada é ignorada e as cores acabam aparecendo
    como texto bruto — por isso o --no-color existe como saída de emergência.
    """
    if platform.system().lower() != "windows":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        modo = ctypes.c_uint32()
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if not kernel32.GetConsoleMode(handle, ctypes.byref(modo)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        novo_modo = modo.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, novo_modo))
    except Exception:
        return False


def colorir(texto, cor):
    """Envolve o texto em códigos ANSI da cor pedida, se as cores estiverem ativas."""
    if not CORES_ATIVAS or cor not in CORES:
        return texto
    return f"{CORES[cor]}{texto}{CORES['reset']}"


def cor_por_faixa(valor, limite_bom, limite_atencao, inverso=False):
    """
    Retorna 'verde', 'amarelo' ou 'vermelho' com base em faixas numéricas.
    Por padrão, quanto menor o valor melhor (ex: perda, latência, jitter).
    """
    if valor is None:
        return "cinza"
    if valor <= limite_bom:
        return "verde"
    if valor <= limite_atencao:
        return "amarelo"
    return "vermelho"


# ---------- PING ----------

def executar_ping(host, quantidade=10, versao_ip=4):
    """Executa ping nativo do SO e extrai estatísticas de perda/latência."""
    sistema = platform.system().lower()
    flag_ipv6 = "-6" if versao_ip == 6 else "-4"

    if sistema == "windows":
        comando = ["ping", flag_ipv6, "-n", str(quantidade), host]
    else:
        comando = ["ping", flag_ipv6, "-c", str(quantidade), host]

    try:
        resultado = subprocess.run(
            comando, capture_output=True, timeout=quantidade * 2 + 5
        )
        saida = decodificar_saida(resultado.stdout)
    except subprocess.TimeoutExpired:
        return None

    return parsear_ping(saida, sistema)


def parsear_ping(saida, sistema):
    """Extrai perda de pacotes e tempos de resposta do output do ping."""
    dados = {
        "perda_pct": None,
        "min_ms": None,
        "avg_ms": None,
        "max_ms": None,
        "jitter_ms": None,
        "tempos": [],
    }

    match_perda = re.search(r"(\d+)%", saida)
    if match_perda:
        dados["perda_pct"] = int(match_perda.group(1))

    # Tempos individuais de resposta (ex: "time=23.4 ms" ou "tempo=23ms")
    tempos = re.findall(r"time[=<]([\d.]+)\s*ms", saida, re.IGNORECASE)
    if not tempos:
        tempos = re.findall(r"tempo[=<]([\d.]+)\s*ms", saida, re.IGNORECASE)

    tempos = [float(t) for t in tempos]
    dados["tempos"] = tempos

    if tempos:
        dados["min_ms"] = min(tempos)
        dados["max_ms"] = max(tempos)
        dados["avg_ms"] = statistics.mean(tempos)
        if len(tempos) > 1:
            # Jitter aproximado: desvio padrão das variações entre pings consecutivos
            variacoes = [abs(tempos[i] - tempos[i - 1]) for i in range(1, len(tempos))]
            dados["jitter_ms"] = statistics.mean(variacoes)

    return dados


# ---------- TRACEROUTE ----------

def executar_traceroute(host, max_saltos=15, versao_ip=4):
    """Executa traceroute/tracert nativo do SO."""
    sistema = platform.system().lower()
    flag_ipv6 = "-6" if versao_ip == 6 else "-4"

    if sistema == "windows":
        comando = ["tracert", flag_ipv6, "-h", str(max_saltos), "-w", "1000", host]
    else:
        comando = ["traceroute", flag_ipv6, "-m", str(max_saltos), "-w", "1", host]

    try:
        resultado = subprocess.run(
            comando, capture_output=True, timeout=max_saltos * 3 + 5
        )
        return decodificar_saida(resultado.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


# Regex simples para detectar IPv4 e IPv6 numa linha de traceroute
REGEX_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
REGEX_IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}\b")


def enriquecer_traceroute_com_nomes(saida):
    """
    Para cada linha do traceroute que só mostra o IP (sem hostname resolvido
    pelo próprio comando), tenta resolver o DNS reverso e anexa o nome.
    """
    if not saida:
        return saida

    linhas_novas = []
    for linha in saida.splitlines():
        ip_encontrado = None

        match_v4 = REGEX_IPV4.search(linha)
        match_v6 = REGEX_IPV6.search(linha)

        if match_v4:
            ip_encontrado = match_v4.group(0)
        elif match_v6 and match_v6.group(0).count(":") >= 2:
            ip_encontrado = match_v6.group(0)

        if ip_encontrado and ip_encontrado not in linha.replace(ip_encontrado, "", 1):
            # já tem algo além do IP na linha? verifica se já existe um nome (letras) antes do IP
            texto_antes_ip = linha.split(ip_encontrado)[0]
            ja_tem_nome = bool(re.search(r"[a-zA-Z]{2,}", texto_antes_ip.split()[-1]) if texto_antes_ip.split() else False)

            if not ja_tem_nome:
                nome = resolver_nome_reverso(ip_encontrado)
                if nome:
                    linha = f"{linha}  [{nome}]"

        linhas_novas.append(linha)

    return "\n".join(linhas_novas)


# ---------- NSLOOKUP / DNS ----------

def executar_nslookup(host):
    """
    Executa o nslookup nativo do SO. Se o comando não existir (comum em
    instalações mínimas de Linux sem o pacote dnsutils/bind-utils),
    cai no fallback via socket puro.
    """
    try:
        resultado = subprocess.run(
            ["nslookup", host], capture_output=True, timeout=8
        )
        saida = decodificar_saida(resultado.stdout)
        if saida.strip():
            return saida
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return nslookup_fallback(host)


def nslookup_fallback(host):
    """
    Alternativa sem depender do comando nslookup: resolve A, AAAA e,
    se possível, o PTR (DNS reverso) usando apenas a stdlib.
    """
    linhas = [f"[fallback via socket — comando 'nslookup' não encontrado]", ""]

    try:
        registros = socket.getaddrinfo(host, None)
    except socket.gaierror:
        linhas.append(f"Não foi possível resolver: {host}")
        return "\n".join(linhas)

    ipv4s, ipv6s = set(), set()
    for familia, _, _, _, endereco in registros:
        if familia == socket.AF_INET:
            ipv4s.add(endereco[0])
        elif familia == socket.AF_INET6:
            ipv6s.add(endereco[0])

    if ipv4s:
        linhas.append("Endereços IPv4 (A):")
        for ip in sorted(ipv4s):
            linhas.append(f"  {ip}")

    if ipv6s:
        linhas.append("Endereços IPv6 (AAAA):")
        for ip in sorted(ipv6s):
            linhas.append(f"  {ip}")

    # DNS reverso do primeiro IP encontrado, como informação extra
    primeiro_ip = next(iter(ipv4s), None) or next(iter(ipv6s), None)
    if primeiro_ip:
        nome_reverso = resolver_nome_reverso(primeiro_ip)
        if nome_reverso:
            linhas.append(f"\nPTR (DNS reverso) de {primeiro_ip}: {nome_reverso}")

    return "\n".join(linhas)


# ---------- CHECAGEM DE PORTAS ----------

def checar_porta(host, porta, timeout=1.0):
    familia = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        sock = socket.socket(familia, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        aberta = sock.connect_ex((host, porta)) == 0
        sock.close()
        return porta, aberta
    except Exception:
        return porta, False


def checar_portas(host, portas_dict):
    """Checa em paralelo um dicionário {porta: nome} qualquer (gerência ou customizado)."""
    if not portas_dict:
        return {}
    resultados = {}
    with ThreadPoolExecutor(max_workers=len(portas_dict)) as executor:
        futures = [executor.submit(checar_porta, host, p) for p in portas_dict]
        for future in futures:
            porta, aberta = future.result()
            resultados[porta] = aberta
    return resultados


def checar_portas_gerencia(host):
    return checar_portas(host, PORTAS_GERENCIA)


def parsear_portas_customizadas(texto):
    """
    Converte a string do --portas em um dict {porta: nome}.
    Aceita formatos: "1723", "1723:PPTP", "53,8080:MeuServico,1723"
    Itens inválidos (não numéricos) são ignorados silenciosamente.
    """
    portas = {}
    if not texto:
        return portas

    for item in texto.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            porta_str, nome = item.split(":", 1)
            nome = nome.strip() or None
        else:
            porta_str, nome = item, None

        try:
            porta = int(porta_str.strip())
        except ValueError:
            continue

        portas[porta] = nome or f"Custom ({porta})"

    return portas


# ---------- CLASSIFICAÇÃO DO PROBLEMA ----------

def classificar_problema(ping_dados, portas_abertas):
    """
    Retorna (mensagem, nivel), onde nivel é 'ok', 'atencao', 'critico' ou
    'indefinido' — usado pra colorir a hipótese no relatório.
    Importante: a classificação usa só as portas de GERÊNCIA fixas, não
    portas customizadas do --portas, pra não distorcer o diagnóstico.
    """
    perda = ping_dados.get("perda_pct")
    jitter = ping_dados.get("jitter_ms")
    avg = ping_dados.get("avg_ms")
    alguma_porta_gerencia_aberta = any(portas_abertas.values())

    if perda is None:
        return "Não foi possível determinar (host não respondeu ao ping ou resultado inconclusivo).", "indefinido"

    if perda == 100:
        if alguma_porta_gerencia_aberta:
            return ("Inconsistente: sem resposta de ping, mas porta de gerência aberta. "
                    "Verificar firewall/ICMP bloqueado no CPE."), "atencao"
        return ("Equipamento provavelmente OFFLINE (sem resposta de ping e "
                "nenhuma porta de gerência acessível)."), "critico"

    if perda > 20:
        return (f"Perda de pacotes significativa ({perda}%). Possível instabilidade de sinal, "
                "cabeamento ou congestionamento de rede."), "critico"

    if jitter is not None and jitter > 30:
        return (f"Jitter elevado ({jitter:.1f} ms). Possível instabilidade de rede, mesmo com "
                "perda de pacotes baixa — atenção a aplicações sensíveis (VoIP, jogos, streaming)."), "atencao"

    if avg is not None and avg > 150:
        return (f"Latência alta ({avg:.1f} ms de média). Verificar rota/congestionamento — "
                "ver resultado do traceroute para identificar o salto problemático."), "atencao"

    if not alguma_porta_gerencia_aberta:
        return ("Ping normal, mas nenhuma porta de gerência acessível. Roteador pode estar "
                "travado/sem acesso remoto — considerar reboot."), "atencao"

    return "Sem indícios de problema de conectividade. Verificar configuração ou aplicação específica do cliente.", "ok"


# ---------- RELATÓRIO ----------

def imprimir_bloco_portas(titulo, portas_dict, portas_abertas):
    """Imprime uma seção de portas (gerência ou customizada) já colorida por status."""
    print(f"\n[ {titulo} ]")
    for porta, nome in portas_dict.items():
        aberta = portas_abertas.get(porta, False)
        if aberta:
            status = colorir("ABERTA", "verde")
        else:
            status = colorir("fechada/indisponível", "vermelho")
        print(f"  {porta:>5} ({nome:<18}): {status}")


def imprimir_relatorio(host, ping_dados, portas_abertas, hipotese_info, portas_extras=None, portas_extras_abertas=None):
    hipotese, nivel = hipotese_info
    cor_nivel = {"ok": "verde", "atencao": "amarelo", "critico": "vermelho", "indefinido": "cinza"}.get(nivel, "cinza")

    print("\n" + "=" * 55)
    print(colorir(f" DIAGNÓSTICO DE CONEXÃO — {host}", "negrito"))
    print("=" * 55)

    print("\n[ PING ]")
    perda = ping_dados.get("perda_pct")
    if perda is not None:
        cor_perda = cor_por_faixa(perda, limite_bom=0, limite_atencao=20)
        print(f"  Perda de pacotes : {colorir(f'{perda}%', cor_perda)}")

    if ping_dados.get("min_ms") is not None:
        avg = ping_dados["avg_ms"]
        cor_lat = cor_por_faixa(avg, limite_bom=50, limite_atencao=150)
        print(f"  Latência mín/méd/máx : "
              f"{ping_dados['min_ms']:.1f} / {colorir(f'{avg:.1f}', cor_lat)} / {ping_dados['max_ms']:.1f} ms")

    if ping_dados.get("jitter_ms") is not None:
        jitter = ping_dados["jitter_ms"]
        cor_jitter = cor_por_faixa(jitter, limite_bom=15, limite_atencao=30)
        print(f"  Jitter (aprox.)  : {colorir(f'{jitter:.1f} ms', cor_jitter)}")

    if not ping_dados.get("tempos"):
        print(colorir("  Sem respostas — host não respondeu a nenhum pacote.", "vermelho"))

    imprimir_bloco_portas("PORTAS DE GERÊNCIA", PORTAS_GERENCIA, portas_abertas)

    if portas_extras:
        imprimir_bloco_portas("PORTAS CUSTOMIZADAS", portas_extras, portas_extras_abertas or {})

    print("\n[ HIPÓTESE DIAGNÓSTICA ]")
    print(f"  {colorir(hipotese, cor_nivel)}")
    print("\n" + "=" * 55)


# ---------- MODO WATCH ----------

def resumo_estado(ping_dados, portas_abertas, hipotese_info):
    """Assinatura enxuta do estado atual, usada pra detectar mudanças entre ciclos do watch."""
    hipotese, nivel = hipotese_info
    return (
        ping_dados.get("perda_pct"),
        round(ping_dados["avg_ms"], 1) if ping_dados.get("avg_ms") is not None else None,
        tuple(sorted(portas_abertas.items())),
        hipotese,
    )


def ciclo_diagnostico(ip, versao_ip, quantidade_pings):
    """Um ciclo rápido: só ping + portas de gerência (sem traceroute/nslookup, mais pesados)."""
    ping_dados = executar_ping(ip, quantidade=quantidade_pings, versao_ip=versao_ip) or {}
    portas_abertas = checar_portas_gerencia(ip)
    hipotese_info = classificar_problema(ping_dados, portas_abertas)
    return ping_dados, portas_abertas, hipotese_info


def modo_watch(host, ip, versao_ip, intervalo, quantidade_pings):
    """
    Repete o diagnóstico (ping + portas) a cada `intervalo` segundos.
    Só imprime um bloco novo quando o estado muda; nos ciclos sem mudança,
    atualiza uma linha única no terminal pra não poluir a tela.
    """
    print(f"\nModo watch ativado — checando {host} a cada {intervalo}s (Ctrl+C pra sair)\n")
    estado_anterior = None

    try:
        while True:
            agora = datetime.now().strftime("%H:%M:%S")
            ping_dados, portas_abertas, hipotese_info = ciclo_diagnostico(ip, versao_ip, quantidade_pings)
            estado_atual = resumo_estado(ping_dados, portas_abertas, hipotese_info)

            if estado_atual != estado_anterior:
                hipotese, nivel = hipotese_info
                cor_nivel = {"ok": "verde", "atencao": "amarelo", "critico": "vermelho", "indefinido": "cinza"}.get(nivel, "cinza")
                perda = ping_dados.get("perda_pct")
                avg = ping_dados.get("avg_ms")
                linha_latencia = f"{avg:.1f}ms" if avg is not None else "s/ resposta"
                cor_perda = cor_por_faixa(perda, limite_bom=0, limite_atencao=20)

                print(colorir(f"[{agora}] ▶ MUDANÇA DE ESTADO", "negrito"))
                print(f"    Perda: {colorir(f'{perda}%', cor_perda)}  |  Latência média: {linha_latencia}")
                print(f"    Hipótese: {colorir(hipotese, cor_nivel)}\n")
                estado_anterior = estado_atual
            else:
                print(f"[{agora}] sem mudanças (perda {ping_dados.get('perda_pct')}%)     ", end="\r")

            time.sleep(intervalo)

    except KeyboardInterrupt:
        print("\n\nModo watch interrompido.")


# ---------- MAIN ----------

def main():
    global CORES_ATIVAS

    parser = argparse.ArgumentParser(description="Diagnóstico rápido de conexão de cliente")
    parser.add_argument("host", help="IP ou hostname do CPE/roteador do cliente")
    parser.add_argument("--pings", type=int, default=10, help="Quantidade de pings (padrão: 10)")
    parser.add_argument("--sem-traceroute", action="store_true",
                         help="Pula a etapa de traceroute (mais rápido)")
    parser.add_argument("--ipv6", action="store_true",
                         help="Força o diagnóstico via IPv6 (exige que o host tenha registro AAAA)")
    parser.add_argument("--sem-nslookup", action="store_true",
                         help="Pula a etapa de nslookup")
    parser.add_argument("--watch", type=int, default=None, metavar="SEGUNDOS",
                         help="Ativa modo contínuo: repete ping+portas a cada N segundos, "
                              "só exibindo quando o status mudar")
    parser.add_argument("--portas", type=str, default=None, metavar="LISTA",
                         help="Portas extras a checar, além das de gerência. "
                              "Formato: '1723,53,8080:MeuServico'")
    parser.add_argument("--no-color", action="store_true",
                         help="Desativa cores na saída (útil em terminais sem suporte ANSI "
                              "ou ao redirecionar pra arquivo)")
    args = parser.parse_args()

    if args.no_color:
        CORES_ATIVAS = False
    else:
        CORES_ATIVAS = habilitar_ansi_windows()

    ip, versao_ip = resolver_host(args.host, forcar_ipv6=args.ipv6)

    if ip is None:
        if args.ipv6:
            print(f"Não foi possível resolver um endereço IPv6 (AAAA) para: {args.host}")
        else:
            print(f"Não foi possível resolver o host: {args.host}")
        return

    print(f"\nDiagnosticando {args.host} ({ip}) — IPv{versao_ip}...")

    if args.watch:
        modo_watch(args.host, ip, versao_ip, args.watch, args.pings)
        return

    portas_extras = parsear_portas_customizadas(args.portas)

    nslookup_saida = None
    if not args.sem_nslookup:
        print("Executando nslookup...")
        nslookup_saida = executar_nslookup(args.host)

    print("Executando ping...")
    ping_dados = executar_ping(ip, quantidade=args.pings, versao_ip=versao_ip) or {}

    print("Checando portas de gerência...")
    portas_abertas = checar_portas_gerencia(ip)

    portas_extras_abertas = {}
    if portas_extras:
        print("Checando portas customizadas...")
        portas_extras_abertas = checar_portas(ip, portas_extras)

    traceroute_saida = None
    if not args.sem_traceroute:
        print("Executando traceroute (pode levar alguns segundos)...")
        traceroute_saida = executar_traceroute(ip, versao_ip=versao_ip)
        traceroute_saida = enriquecer_traceroute_com_nomes(traceroute_saida)

    hipotese_info = classificar_problema(ping_dados, portas_abertas)
    imprimir_relatorio(args.host, ping_dados, portas_abertas, hipotese_info,
                        portas_extras=portas_extras, portas_extras_abertas=portas_extras_abertas)

    if nslookup_saida:
        print("\n[ NSLOOKUP ]")
        print(nslookup_saida)

    if traceroute_saida:
        print("\n[ TRACEROUTE ]")
        print(traceroute_saida)


if __name__ == "__main__":
    main()