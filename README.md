# diagnostico.py

Ferramenta de linha de comando para diagnóstico rápido de conexão de clientes de ISP — voltada para atendimento N2. Roda ping, traceroute, nslookup e checagem de portas de gerência num CPE/roteador, e sugere uma hipótese inicial de problema com base nos resultados.

Sem dependências externas — usa apenas a *standard library* do Python 3.

## Funcionalidades

- **Ping** com estatísticas de perda de pacotes, latência (mín/média/máx) e jitter aproximado.
- **Traceroute** com enriquecimento automático de DNS reverso nos saltos que não vêm resolvidos.
- **nslookup** com fallback via `socket` puro, caso o comando `nslookup` não esteja disponível no sistema.
- **Checagem de portas de gerência** (Telnet, HTTP, HTTPS, TR-069/CWMP, SNMP) via socket TCP.
- **Portas customizadas**: teste ad-hoc de portas extras, sem afetar a hipótese diagnóstica.
- **Hipótese diagnóstica automática**, classificada por severidade (ok / atenção / crítico).
- **Saída colorida** no terminal (verde/amarelo/vermelho), com opção de desativar.
- **Modo watch**: monitoramento contínuo, exibindo apenas mudanças de estado.
- **Suporte a IPv4 e IPv6**.
- **Compatível com Linux e Windows** (detecta encoding de console e comandos nativos do SO).

## Requisitos

- Python 3.8+
- Comandos nativos do sistema: `ping` e `traceroute` (Linux) ou `tracert` (Windows) disponíveis no PATH.
  - `nslookup` é opcional — se ausente, a ferramenta cai automaticamente no fallback via `socket`.

## Instalação

```bash
git clone https://github.com/DefinitelyNotVictor/<nome-do-repo>.git
cd <nome-do-repo>
python3 diagnostico.py --help
```

Nenhuma instalação de pacotes é necessária.

## Uso básico

```bash
python3 diagnostico.py <host>
```

Exemplo:

```bash
python3 diagnostico.py 192.168.0.1
```

Saída (resumida):

```
=======================================================
 DIAGNÓSTICO DE CONEXÃO — 192.168.0.1
=======================================================

[ PING ]
  Perda de pacotes : 0%
  Latência mín/méd/máx : 8.2 / 12.4 / 18.9 ms
  Jitter (aprox.)  : 3.1 ms

[ PORTAS DE GERÊNCIA ]
     23 (Telnet            ): fechada/indisponível
     80 (HTTP (admin web)  ): ABERTA
    443 (HTTPS (admin web) ): ABERTA
   7547 (TR-069 (CWMP)     ): fechada/indisponível
    161 (SNMP              ): fechada/indisponível

[ HIPÓTESE DIAGNÓSTICA ]
  Sem indícios de problema de conectividade. Verificar configuração ou aplicação específica do cliente.
=======================================================
```

## Opções

| Flag | Descrição |
|---|---|
| `host` | IP ou hostname do CPE/roteador do cliente (obrigatório) |
| `--pings N` | Quantidade de pings a enviar (padrão: 10) |
| `--sem-traceroute` | Pula a etapa de traceroute (execução mais rápida) |
| `--sem-nslookup` | Pula a etapa de nslookup |
| `--ipv6` | Força o diagnóstico via IPv6 (exige registro AAAA no host) |
| `--portas LISTA` | Portas extras a checar, além das de gerência. Formato: `1723,53,8080:MeuServico` |
| `--watch SEGUNDOS` | Ativa modo contínuo: repete ping + portas de gerência a cada N segundos, exibindo apenas mudanças de estado |
| `--no-color` | Desativa cores na saída (útil em terminais sem suporte ANSI ou ao redirecionar para arquivo) |

## Exemplos

**Diagnóstico rápido, sem traceroute:**
```bash
python3 diagnostico.py 192.168.0.1 --sem-traceroute
```

**Forçar IPv6:**
```bash
python3 diagnostico.py exemplo.com.br --ipv6
```

**Checar portas extras (ex: PPTP e um serviço customizado):**
```bash
python3 diagnostico.py 192.168.0.1 --portas 1723,8080:PainelCliente
```

**Monitorar continuamente a cada 5 segundos:**
```bash
python3 diagnostico.py 192.168.0.1 --watch 5
```

**Saída sem cores (para redirecionar a um arquivo):**
```bash
python3 diagnostico.py 192.168.0.1 --no-color > relatorio.txt
```

## Como funciona a hipótese diagnóstica

A classificação usa apenas os resultados de ping e das **portas de gerência fixas** (Telnet, HTTP, HTTPS, TR-069, SNMP) — portas customizadas passadas via `--portas` são exibidas separadamente e não influenciam a hipótese, para não distorcer o diagnóstico com testes pontuais.

| Situação | Nível |
|---|---|
| Ping normal, alguma porta de gerência acessível | ok |
| Ping normal, nenhuma porta de gerência acessível | atenção |
| Latência alta (> 150 ms) ou jitter elevado (> 30 ms) | atenção |
| Sem resposta de ping, mas porta de gerência aberta | atenção (possível ICMP bloqueado) |
| Perda de pacotes > 20% | crítico |
| 100% de perda e nenhuma porta acessível | crítico (equipamento offline) |

## Modo watch

Repete um ciclo leve (ping + portas de gerência, sem traceroute/nslookup) a cada N segundos. Só imprime um bloco novo quando o estado muda; enquanto nada muda, atualiza uma única linha no terminal.

```bash
python3 diagnostico.py 192.168.0.1 --watch 5
```

Encerre com `Ctrl+C`.

## Compatibilidade Windows

- Detecta automaticamente o *code page* do console (`cp850`, `cp860`, etc.) para evitar acentuação corrompida na saída de `ping`/`tracert`.
- Tenta habilitar suporte a cores ANSI via `SetConsoleMode`. Caso não seja possível (versões muito antigas do Windows), use `--no-color`.

## Limitações conhecidas

- Depende dos binários nativos `ping`/`traceroute`/`tracert` do sistema operacional.
- O parsing da saída do `ping` é feito via regex sobre o texto — variações de idioma/formato do sistema podem exigir ajustes nos padrões reconhecidos (atualmente cobre saídas em português e inglês).
- `--watch` não executa traceroute nem nslookup a cada ciclo, por design (evita sobrecarregar a rede).

## Licença

Defina a licença do repositório (ex: MIT) conforme sua preferência.
