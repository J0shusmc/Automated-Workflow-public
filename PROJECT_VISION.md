# Automated Workflow Vision

## Core Product Direction

Automated Workflow is a local visual workflow builder that behaves like a notebook for automation. Each node is a runnable cell. Data flows from left to right through connected handles, and each downstream node receives the output from the node before it.

The canvas should support long horizontal workflows. The builder area extends to the right and can scroll as the user keeps adding steps.

## Image Generation Work Order Flow

The first high-value workflow is image generation.

Basic target flow:

1. User enters a short direct prompt.
2. Claudia, the local LLM agent, enhances the prompt into a work order.
3. Claudia creates three prompt variations for the same desired outcome.
4. The work order passes those prompt variations to the image generation action.
5. The image generation script generates images from those prompts.

Current basic implementation path:

```text
User input → Claudia agent → Image Generation
```

Claudia is a permanent workflow node in this chain. It should not disappear after a run; it remains available as the reusable agent/controller step for the workflow.

The user prompt can be short, for example:

```text
a dog running on the beach in ultra 8k
```

The LLM should turn that into richer prompts that preserve the user intent, improve visual specificity, and aim for the best possible 8k-quality image output.

## Claudia Agent

The local LLM agent is named Claudia.

Claudia should have a dedicated agent node that lives at the top of the screen. The agent acts like the boss/coordinator of the workflow:

- Inputs can be sent to Claudia.
- Claudia can produce work orders.
- Claudia can pass structured results to downstream nodes.
- Claudia can enhance prompts before image generation.

## Node Types

Many nodes will be Python scripts. Python action nodes should continue to work like notebook cells:

- Receive `inputs` from upstream nodes.
- Run the selected Python script.
- Emit outputs to downstream nodes.

Action nodes can also bridge notebook inputs into CLI arguments when the selected script is command-line oriented, such as passing an upstream prompt into an image generation script's `--prompt` argument.

## Visual Runtime Behavior

The workflow should feel alive while running.

Desired runtime visuals:

- The currently running node should visibly flash, pulse, or otherwise show active state.
- Links should animate when data is being passed from one node to another.
- Python action nodes should visually show code execution while running.
- For script nodes, the visible code preview should animate while the script runs: scroll downward to the bottom, loop back to the top, and ideally use a polished 3D-style rendering.

## Layout Direction

The current canvas should evolve toward an "Automation Desktop" feeling:

- Workflow building happens horizontally from left to right.
- The builder section should support horizontal scrolling.
- Nodes should remain readable and resizable.
- Workflow History starts collapsed by default.
- Active Workflows starts collapsed by default.

## Current Build Priority

Continuous always-running workflow internals are intentionally deferred until there is a real workflow that needs to run all the time.

Current priority order:

1. Automation Desktop layout and visual upgrades.
2. Runtime visuals for running nodes, data movement, and script activity.
3. Claudia agent node and local LLM integration.
4. Workflow-specific continuous services such as email scanning, payment verification, and file delivery.
