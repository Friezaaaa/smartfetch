import { CdpX402Client } from "@coinbase/cdp-sdk/x402";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { wrapMCPClientWithPayment } from "@x402/mcp";

const MCP_URL =
  process.env.SMARTFETCH_MCP_URL ??
  "https://smartfetch-production-ea53.up.railway.app/mcp";
const TARGET_URL = process.env.SMARTFETCH_TARGET_URL ?? "https://example.com/";
const BASE_MAINNET = "eip155:8453";
const BASE_MAINNET_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913";
const MAX_PAYMENT_ATOMIC = 5_000n;

async function main(): Promise<void> {
  const payment = new CdpX402Client({
    environment: "production",
    spendControls: {
      maxAmountPerPayment: {
        atomic: MAX_PAYMENT_ATOMIC,
        asset: BASE_MAINNET_USDC,
      },
      maxCumulativeSpend: {
        atomic: MAX_PAYMENT_ATOMIC,
        asset: BASE_MAINNET_USDC,
      },
      maxCumulativeSpendWindow: "24h",
      allowedNetworks: [BASE_MAINNET],
    },
  });

  const { evmAddress } = await payment.getAddresses();
  console.log("Paying from:", evmAddress);
  console.log("Maximum payment: $0.005 USDC on Base mainnet");

  // CDP currently publishes its x402 declaration through CommonJS while
  // @x402/mcp publishes ESM declarations. They share the same runtime client,
  // but TypeScript treats their private fields as separate nominal types.
  const mcpPayment = payment as unknown as Parameters<
    typeof wrapMCPClientWithPayment
  >[1];

  const mcp = wrapMCPClientWithPayment(
    new Client(
      { name: "smartfetch-buyer", version: "1.0.0" },
      { capabilities: {} },
    ),
    mcpPayment,
    { autoPayment: true },
  );

  await mcp.connect(new StreamableHTTPClientTransport(new URL(MCP_URL)));
  try {
    const listed = await mcp.listTools();
    console.log(
      "Free tools/list:",
      listed.tools.map((tool) => tool.name),
    );
    console.log("Calling fetch_webpage; this can spend real USDC.");
    const result = await mcp.callTool("fetch_webpage", {
      url: TARGET_URL,
      max_chars: 20_000,
      force_browser: false,
    });
    console.log("Result:", JSON.stringify(result.content, null, 2));
    console.log("Payment made:", result.paymentMade);
    console.log(
      "Settlement receipt:",
      JSON.stringify(result.paymentResponse ?? null, null, 2),
    );
  } finally {
    await mcp.close();
  }
}

main().catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
