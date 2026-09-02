import { createMcpAdapter } from "pi-mcp-adapter"

export default createMcpAdapter({
  config: {
    mcpServers: {
      "peer-review-agents": {
        command: "peerreview-mcp",
        args: [],
        lifecycle: "lazy",
      },
    },
  },
})
