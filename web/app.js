const state = { workflow: { id: null, name: 'Untitled workflow', nodes: [], edges: [] }, selected: null, linking: null, pendingLinkSource: null, runs: {}, copiedWorkflow: null, runningNode: null, transmittingEdges: new Set(), flashNodes: new Set(), serviceEventKeys: new Set() };
const $ = (selector) => document.querySelector(selector);
const canvas = $('#canvas');
const world = $('#canvas-world');
const codeScroll = { nodeId: null, startedAt: 0, raf: null };
let runEventQueue = Promise.resolve();
let nodeVisualStartedAt = 0;
let activeRunMode = 'whole';
let activeTerminalSession = null;
let activeRunId = null;
let activeRunStream = null;
let terminalEventSource = null;
const codexProjectsBySession = new Map();
const codexEditsBySession = new Map();
const codexAgentBuildsBySession = new Map();
const llmDeltaCards = new Map();
const llmNodeTargets = new Map();
const llmNodeStreamState = new Map();
let serviceEventsPrimed = false;
let lastLlmActivityId = 0;
let pendingLocalLlmNodeId = null;
let layoutSaveTimer = null;
let workflowRevision = 0;
let saveInFlight = false;
let saveQueued = false;
let savePromise = null;
let workflowClickTimer = null;
let terminalAutoCloseTimer = null;
let workflowDrag = null;
const activeWorkflowRunStreams = new Set();
const recurringWorkflowTerminalRuns = new Set();
const workflowRunIds = new Map();
const workflowRunEventSources = new Map();
const workflowTerminals = new Map();
const workflowTerminalSessions = new Map();
const workflowTerminalEventSources = new Map();
const WORKFLOW_TERMINAL_IDLE_MS = 10000;
const DRAFT_AGENT_PLACEHOLDER = 'Tell me what the agent should do. Example: improve a short image prompt while preserving the exact subject and adding only directly relevant visual quality details.';
const DEFAULT_PRODUCT_ROOT = 'data/products';
const DEFAULT_DATA_ROOT = 'data';

function id() { return crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2); }
function names(value) { return value.split(',').map(x => x.trim()).filter(Boolean); }
function markDirty() { workflowRevision += 1; $('#save-state').textContent = 'Unsaved changes'; }
function scheduleLayoutSave() { if(!state.workflow.id) return; clearTimeout(layoutSaveTimer); layoutSaveTimer=window.setTimeout(()=>save().catch(console.warn),450); }
function syncBottomServiceHeight() { const panel=$('#bottom-service-panel'); if(panel) document.documentElement.style.setProperty('--bottom-service-height', `${Math.ceil(panel.getBoundingClientRect().height)}px`); }
function nodeById(nodeId) { return state.workflow.nodes.find(node => node.id === nodeId); }
function agentBuilder(node) { return node?.agent?.builder || (node?.agent?.codexDraft ? 'codex' : 'claudia'); }
function isDraftAgent(node) { return node?.type === 'agent' && (node.agent?.draft || node.agent?.claudiaDraft || node.agent?.codexDraft); }
function portKey(nodeId, port) { return `${nodeId}:${port}`; }
function edgeKey(edge) { return `${edge.source}:${edge.sourceHandle}->${edge.target}:${edge.targetHandle}`; }
function displayNodeName(node) { return (node.name || '').replace(/\s+\d+$/, ''); }
function escapeHtml(value) { return value.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'); }
function ansiToHtml(value) {
  const colors={30:'ansi-black',31:'ansi-red',32:'ansi-green',33:'ansi-yellow',34:'ansi-blue',35:'ansi-magenta',36:'ansi-cyan',37:'ansi-white',90:'ansi-dim',91:'ansi-red',92:'ansi-green',93:'ansi-yellow',94:'ansi-blue',95:'ansi-magenta',96:'ansi-cyan',97:'ansi-white'};
  let html='', last=0, active=[];
  const pattern=/\x1b\[([0-9;]*)m/g;
  for(let match; (match=pattern.exec(value));){
    html+=escapeHtml(value.slice(last,match.index));
    const codes=(match[1]||'0').split(';').map(code=>Number(code||0));
    if(codes.includes(0)){if(active.length) html+='</span>';active=[];}
    const next=codes.map(code=>colors[code]).filter(Boolean);
    if(next.length){if(active.length) html+='</span>';active=next;html+=`<span class="${active.join(' ')}">`;}
    last=pattern.lastIndex;
  }
  html+=escapeHtml(value.slice(last));
  if(active.length) html+='</span>';
  return html;
}
function terminalRichHtml(value) {
  const text=typeof value==='string' ? value : JSON.stringify(value, null, 2);
  return ansiToHtml(text)
    .replace(/^([A-Z][A-Z0-9._-]{0,8})(?=\s+\$?\d|\s{2,}|$)/gm,'<span class="ticker">$1</span>')
    .replace(/([+-]\d+(?:\.\d+)?%?)/g, value=>`<span class="${value.startsWith('-')?'negative':'positive'}">${value}</span>`);
}
function imageArtifact(value) { if(!value || typeof value !== 'object') return null; if(value.type === 'image' && value.url) return value; if(value.image && typeof value.image === 'object' && value.image.url) return value.image; return null; }
function mediaArtifact(value) { if(!value || typeof value !== 'object') return null; if(['image','video'].includes(value.type) && value.url) return value; if(value.image?.url) return value.image; if(value.video?.url) return value.video; return null; }
function outputKind(node) { const outputs=node?.outputs||[]; if(outputs.includes('image')) return 'image'; if(outputs.includes('video')) return 'video'; if(outputs.includes('file') || node?.type==='file') return 'file'; if(outputs.includes('product') || node?.type==='product') return 'product'; if(node?.task?.kind==='image-generation' || node?.code?.includes('/sdapi/v1/txt2img')) return 'image'; if(node?.task?.kind==='video-generation' || node?.code?.includes('--work-order')) return 'video'; if(node?.type==='input') return 'prompt'; if(node?.type==='agent') return 'prompt'; return 'result'; }
function preferredTargetHandle(source, target) { const inputs=target?.inputs||['input']; const open=inputs.filter(port=>!state.workflow.edges.some(edge=>edge.target===target.id && (edge.targetHandle||'input')===port)); const candidates=open.length?open:inputs; const kind=outputKind(source); if(kind==='image') return candidates.find(port=>['reference_image','image','init_image'].includes(port)) || candidates[0]; if(kind==='file') return candidates.find(port=>['csv','file','file_path','path','input','data'].includes(port)) || candidates[0]; if(kind==='product') return candidates.find(port=>['product','product_path','file','folder'].includes(port)) || candidates[0]; if(kind==='prompt') return candidates.find(port=>['prompt','video_prompt','input','text'].includes(port)) || candidates[0]; return candidates[0]; }
function renameNodePrompt(nodeId) { const node=nodeById(nodeId); if(!node)return; const name=prompt('Rename node', node.name || 'Node'); if(name == null)return; const next=name.trim(); if(!next)return; node.name=next; markDirty(); render(); }
function normalizeTaskPorts(node) { if(node?.type!=='task') return; if(node.code?.includes('--work-order')){node.task={...(node.task||{}),kind:'video-generation'}; if(!(node.inputs||[]).includes('prompt') || !(node.inputs||[]).includes('reference_image')) node.inputs=['prompt','reference_image']; if(!(node.outputs||[]).includes('video')) node.outputs=['video'];} else if(node.code?.includes('/sdapi/v1/txt2img')){node.task={...(node.task||{}),kind:'image-generation'}; if(!(node.inputs||[]).includes('prompt')) node.inputs=['prompt']; if(!(node.outputs||[]).includes('image')) node.outputs=['image'];} }
function highlightPython(code) { const token=/(#.*$)|((?:f|r|b|u)?(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'))|\b(def|class|import|from|as|return|if|elif|else|for|while|in|try|except|finally|with|lambda|yield|async|await|pass|break|continue|raise|global|nonlocal|assert|match|case)\b|\b(True|False|None)\b|\b(\d+(?:\.\d+)?)\b/gm; return code.split('\n').map(line=>{let html='',last=0,match; token.lastIndex=0; while((match=token.exec(line))){html+=escapeHtml(line.slice(last,match.index));const kind=match[1]?'comment':match[2]?'string':match[3]?'keyword':match[4]?'constant':'number';html+=`<span class="py-${kind}">${escapeHtml(match[0])}</span>`;last=token.lastIndex;}return html+escapeHtml(line.slice(last));}).join('\n'); }
function codeMap(lines) { return lines.map(line=>{const kind=/^\s*#/.test(line)?'comment':/\b(def|class|import|from|return|if|for|while|try|async)\b/.test(line)?'keyword':/["']/.test(line)?'string':/\b\d/.test(line)?'number':'plain'; const width=Math.min(98,Math.max(10,line.trim().length*1.45));return `<span class="code-map-line py-${kind}" style="--map-width:${width}%"></span>`;}).join(''); }

async function request(url, options) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Request failed');
  return body;
}

function selectedProduct(node) {
  const product = node.product || {};
  const products = Array.isArray(product.products) ? product.products : [];
  return products.find(item => item.name === product.selected) || products[0] || null;
}

function productPayload(item) {
  if(!item) return {};
  return {product:item.name, product_name:item.name, product_path:item.path, product_file:item.path, fileCount:item.fileCount || 0};
}

function dataFileChoices(node) {
  const nodeFiles = Array.isArray(node?.file?.files) ? node.file.files : [];
  const libraryFiles = Array.isArray(state.dataFiles?.files) ? state.dataFiles.files : [];
  if(libraryFiles.length) return [...libraryFiles].sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
  const byPath = new Map();
  nodeFiles.forEach(item => {
    if(item?.path) byPath.set(item.path, item);
  });
  return [...byPath.values()].sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
}

function selectedDataFile(node) {
  const file = node.file || {};
  const files = dataFileChoices(node);
  return files.find(item => item.path === file.selected) || files[0] || null;
}

function dataFilePayload(item) {
  if(!item) return {};
  return {type:'file', name:item.name, path:item.path, relativePath:item.relativePath, file:item.path, file_path:item.path, content:item.content || '', text:item.content || ''};
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || '').split(',')[1] || '');
    reader.onerror = () => reject(reader.error || new Error('File read failed'));
    reader.readAsDataURL(file);
  });
}

async function uploadFilesToProduct(node, files) {
  const selected = selectedProduct(node);
  if(!selected) throw new Error('Choose a product folder first.');
  for(const file of files) {
    const contentBase64 = await fileToBase64(file);
    await request('/api/products/upload', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({product:selected.name, fileName:file.name, contentBase64}),
    });
  }
  const data = await refreshProducts(node);
  const refreshed = (data.products || []).find(item => item.name === selected.name);
  node.product = {...node.product, selected:selected.name};
  if(refreshed) node.product.products = data.products;
  markDirty();
  render();
}

async function uploadDataFiles(node, files) {
  let uploadedPath = '';
  for(const file of files) {
    const contentBase64 = await fileToBase64(file);
    const uploaded = await request('/api/data-files/upload', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({fileName:file.name, contentBase64}),
    });
    uploadedPath = uploaded.file?.path || uploadedPath;
  }
  const data = await refreshDataFiles(node);
  node.file = {...node.file, selected:uploadedPath || node.file?.selected || data.files?.[0]?.path || '', showChoices:false};
  markDirty();
  render();
}

async function refreshDataFiles(targetNode=null) {
  const data = await request('/api/data-files');
  state.dataFiles = data;
  if(targetNode) {
    const files = data.files || [];
    const selected = files.some(item => item.path === targetNode.file?.selected)
      ? targetNode.file.selected
      : files[0]?.path || '';
    targetNode.file = {
      root: data.root || DEFAULT_DATA_ROOT,
      files,
      selected,
      showChoices: targetNode.file?.showChoices || false,
    };
    if(!targetNode.name || targetNode.name === 'Data file') targetNode.name = 'Data file';
  }
  return data;
}

async function refreshProducts(targetNode=null) {
  const data = await request('/api/products');
  state.products = data;
  if(targetNode) {
    targetNode.product = {
      root: data.root || DEFAULT_PRODUCT_ROOT,
      products: data.products || [],
      selected: targetNode.product?.selected || data.products?.[0]?.name || '',
    };
    if(!targetNode.name || targetNode.name === 'Product folders') targetNode.name = 'Product';
  }
  return data;
}

function renderProductPreview(node, preview) {
  const product = node.product || {};
  const products = Array.isArray(product.products) ? product.products : [];
  preview.replaceChildren();
  preview.classList.add('product-preview');
  if(!products.length) {
    const empty = document.createElement('div');
    empty.className = 'product-empty';
    empty.textContent = `No folders found in ${product.root || DEFAULT_PRODUCT_ROOT}`;
    preview.append(empty);
    return;
  }
  const list = document.createElement('div');
  list.className = 'product-folder-list';
  products.forEach(item => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `product-folder ${item.name === product.selected ? 'selected' : ''}`;
    button.innerHTML = '<span>□</span><b></b><small></small>';
    button.querySelector('b').textContent = item.name;
    button.querySelector('small').textContent = `${item.fileCount || 0} files`;
    button.onclick = event => {
      event.stopPropagation();
      node.product = {...product, selected:item.name};
      markDirty();
      render();
    };
    list.append(button);
  });
  const add = document.createElement('button');
  add.type = 'button';
  add.className = 'product-add-file';
  add.textContent = 'Add files';
  add.onclick = event => {
    event.stopPropagation();
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.onchange = () => uploadFilesToProduct(node, [...input.files]).catch(error => alert(error.message));
    input.click();
  };
  preview.append(list, add);
}

function renderDataFilePreview(node, preview) {
  const file = node.file || {};
  const files = dataFileChoices(node);
  const selected = selectedDataFile(node);
  const showChoices = file.showChoices || !selected;
  preview.replaceChildren();
  preview.classList.add('data-file-preview');
  if(!files.length) {
    const empty = document.createElement('div');
    empty.className = 'product-empty';
    empty.textContent = `No CSV, JSON, or text files found in ${file.root || DEFAULT_DATA_ROOT}/files`;
    preview.append(empty);
  } else {
    if(selected) {
      const current = document.createElement('button');
      current.type = 'button';
      current.className = 'product-folder data-file-choice selected';
      current.innerHTML = '<span>▤</span><b></b><small></small>';
      current.querySelector('b').textContent = selected.name;
      current.querySelector('small').textContent = `${selected.root || 'data'} · ${selected.relativePath || selected.path}`;
      current.onclick = event => {
        event.stopPropagation();
        node.file = {...file, showChoices:!showChoices};
        render();
      };
      preview.append(current);
    }
    if(showChoices) {
      const list = document.createElement('div');
      list.className = 'product-folder-list';
      files.filter(item => item.path !== selected?.path).forEach(item => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'product-folder data-file-choice';
        button.innerHTML = '<span>▤</span><b></b><small></small>';
        button.querySelector('b').textContent = item.name;
        button.querySelector('small').textContent = `${item.root || 'data'} · ${item.relativePath || item.path}`;
        button.onclick = event => {
          event.stopPropagation();
          node.file = {...file, root:state.dataFiles?.root || file.root || DEFAULT_DATA_ROOT, files, selected:item.path, showChoices:false};
          markDirty();
          render();
        };
        list.append(button);
      });
      preview.append(list);
    }
    const change = document.createElement('button');
    change.type = 'button';
    change.className = 'product-add-file';
    change.textContent = showChoices ? 'Hide data files' : 'Change data file';
    change.onclick = event => {
      event.stopPropagation();
      node.file = {...file, showChoices:!showChoices};
      render();
    };
    preview.append(change);
  }
  const add = document.createElement('button');
  add.type = 'button';
  add.className = 'product-add-file';
  add.textContent = 'Add to data files';
  add.onclick = event => {
    event.stopPropagation();
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.accept = '.csv,.json,.jsonl,.txt,.md,text/csv,application/json,text/plain,text/markdown';
    input.onchange = () => uploadDataFiles(node, [...input.files]).catch(error => alert(error.message));
    input.click();
  };
  preview.append(add);
}

function nodeEl(node) {
  normalizeTaskPorts(node);
  const fragment = $('#node-template').content.cloneNode(true);
  const element = fragment.querySelector('.node');
  element.dataset.id = node.id; element.style.left = `${node.x}px`; element.style.top = `${node.y}px`;
  const isInput=node.type==='input';
  const isOutput=node.type==='output';
  const isAgent=node.type==='agent';
  const isBreakpoint=node.type==='breakpoint';
  const isKillClaudia=node.type==='kill-claudia';
  const isCondition=node.type==='condition';
  const isProduct=node.type==='product';
  const isFile=node.type==='file';
  if (isInput && node.inputValue == null && (node.inputs || []).length) { node.inputValue=node.inputs.join('\n'); node.inputs=[]; markDirty(); }
  if (isInput) element.classList.add('input-node');
  if (isAgent) element.classList.add('agent-node', agentBuilder(node)==='codex' ? 'codex-agent-node' : 'claudia-agent-node');
  if (isOutput) element.classList.add('output-node');
  if (isBreakpoint) element.classList.add('breakpoint-node');
  if (isKillClaudia) element.classList.add('kill-claudia-node');
  if (isCondition) element.classList.add('condition-node');
  if (isProduct) element.classList.add('product-node');
  if (isFile) element.classList.add('file-node');
  if (node.includeInWorkflow) element.classList.add('included-node');
  const selectedProductName = selectedProduct(node)?.name || '';
  const selectedFileName = selectedDataFile(node)?.name || '';
  const titleText = isProduct && (!node.name || node.name === selectedProductName || node.name === 'Product folders') ? 'Product' : isFile && (!node.name || node.name === selectedFileName || node.name === 'Data file') ? 'Data file' : isBreakpoint && node.breakpoint?.cachedOutputs ? 'Resume workflow' : displayNodeName(node);
  element.querySelector('strong').textContent = titleText;
  const run = state.runs[node.id];
  const outputValue = run?.outputs?.result ?? node.displayValue ?? 'Waiting for input...';
  const code = isProduct ? JSON.stringify(productPayload(selectedProduct(node)), null, 2) : isFile ? JSON.stringify(dataFilePayload(selectedDataFile(node)), null, 2) : isOutput ? (typeof outputValue === 'string' ? outputValue : JSON.stringify(outputValue, null, 2)) : (node.code || '# Empty code node');
  const lines=(isInput ? (node.inputValue || '').split('\n') : code.split('\n'));
  element.querySelector('.code-scale').textContent=isInput ? 'user input' : isAgent ? 'agent prompt' : isOutput ? 'passthrough' : isKillClaudia ? 'vram release' : isCondition ? 'branch' : isFile ? `${(node.file?.files||[]).length} files` : isProduct ? `${(node.product?.products||[]).length} folders` : `${lines.length} ${lines.length === 1 ? 'line' : 'lines'}`;
  const availableCodeHeight=Math.max(220, Math.min(420, canvas.clientHeight - 180));
  const portHeight=0;
  const defaultWidth=isInput ? 330 : isAgent ? 360 : isOutput ? 520 : (isProduct || isFile) ? 290 : 235;
  const nodeWidth=node.width || defaultWidth;
  const inputCharactersPerLine=Math.max(34, Math.floor((nodeWidth - 54) / 7));
  const visualInputLines=lines.reduce((total,line)=>total+Math.max(1,Math.ceil(line.length/inputCharactersPerLine)),0);
  const inputHeight=Math.min(260,Math.max(64,visualInputLines*20+26));
  const codeHeight=Math.min(availableCodeHeight, Math.max(220, lines.length * 18 + 18));
  const productHeight=190;
  const defaultHeight=isInput ? 43 + portHeight + inputHeight : (isProduct || isFile) ? 43 + portHeight + productHeight : 43 + portHeight + codeHeight;
  element.style.width=`${nodeWidth}px`;
  element.style.height=`${node.userSizedHeight && node.height ? node.height : defaultHeight}px`;
  const preview=element.querySelector('.node-code-preview'); const inputValue=element.querySelector('.node-input-value');
  preview.addEventListener('pointerdown', event=>event.stopPropagation());
  preview.addEventListener('click', event=>event.stopPropagation());
  preview.hidden=isInput || isAgent || isBreakpoint; inputValue.hidden=!(isInput || isAgent);
  if (isBreakpoint) { element.style.top='0px'; element.style.width='24px'; element.style.height=`${Math.max(world.offsetHeight || 0, canvas.scrollHeight || 0, 1100)}px`; element.querySelector('.code-scale').textContent=node.breakpoint?.cachedOutputs?'cached':'break'; }
  if (isInput) { inputValue.value=node.inputValue || ''; inputValue.addEventListener('click', event=>event.stopPropagation()); inputValue.addEventListener('input', event=>{node.inputValue=event.target.value;if(!node.userSizedHeight) delete node.height;markDirty();}); inputValue.addEventListener('keydown', event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();inputValue.blur();}}); inputValue.addEventListener('blur', ()=>{if(!node.userSizedHeight)render();}); }
  else if (isAgent) { inputValue.value=node.code || ''; inputValue.placeholder=isDraftAgent(node) ? DRAFT_AGENT_PLACEHOLDER : ''; inputValue.addEventListener('click', event=>event.stopPropagation()); inputValue.addEventListener('focus', ()=>{if(isDraftAgent(node) && inputValue.value===DRAFT_AGENT_PLACEHOLDER){inputValue.value='';node.code='';node.agent={...(node.agent||{}),systemPrompt:''};markDirty();}}); inputValue.addEventListener('input', event=>{node.code=event.target.value;node.agent={...(node.agent||{}),systemPrompt:event.target.value};if(!node.userSizedHeight) delete node.height;markDirty();}); inputValue.addEventListener('keydown', event=>{if(event.key==='Enter'&&!event.shiftKey&&isDraftAgent(node)){event.preventDefault();node.code=event.target.value;node.agent={...(node.agent||{}),systemPrompt:event.target.value};inputValue.blur();buildDraftAgent(node.id);}}); inputValue.addEventListener('blur', ()=>{if(!node.userSizedHeight)render();}); }
  else if (isOutput) { const artifact=mediaArtifact(outputValue); if(artifact){preview.replaceChildren();preview.classList.add('image-output-preview');preview.classList.remove('terminal-output-preview');const link=document.createElement('a');link.href=artifact.url;link.target='_blank';link.rel='noopener';link.className='image-artifact';link.title=`Open ${artifact.type}`;link.addEventListener('click', event=>event.stopPropagation());let media;if(artifact.type==='video'){media=document.createElement('video');media.src=artifact.url;media.controls=true;media.muted=true;media.addEventListener('click', event=>event.stopPropagation());}else{media=document.createElement('img');media.src=artifact.url;media.alt=artifact.name || 'Generated image';}const caption=document.createElement('span');caption.textContent=artifact.name || `Open generated ${artifact.type}`;link.append(media,caption);preview.append(link);} else {preview.classList.remove('image-output-preview');preview.classList.add('terminal-output-preview');preview.innerHTML = terminalRichHtml(outputValue);} }
  else if (isCondition) { preview.classList.remove('image-output-preview'); preview.textContent = `if output contains:\n${node.condition?.matchText || node.code || 'yes'}\n\ntrue -> if_yes\nfalse -> if_no`; }
  else if (isProduct) { preview.classList.remove('image-output-preview','terminal-output-preview'); renderProductPreview(node, preview); }
  else if (isFile) { preview.classList.remove('image-output-preview','terminal-output-preview'); renderDataFilePreview(node, preview); }
  else preview.innerHTML = highlightPython(code);
  if (run) element.querySelector('.status').classList.add(run.status);
  if (state.runningNode === node.id) element.classList.add('running');
  if (state.runningNode === node.id && !isInput && !isOutput && !isProduct && !isFile) element.classList.add('code-scrolling');
  if (state.flashNodes.has(node.id)) element.classList.add('flash');
  if (state.pendingLinkSource === node.id) element.classList.add('link-source');
  if (state.pendingLinkSource && canLinkExisting(nodeById(state.pendingLinkSource), node)) element.classList.add('link-target');
  for (const port of node.inputs || []) element.querySelector('.inputs').append(portEl(port, 'input', node.id));
  for (const port of node.outputs || ['result']) element.querySelector('.outputs').append(portEl(port, 'output', node.id));
  const hasOutgoing=state.workflow.edges.some(edge => edge.source === node.id);
  const hasOpenConditionOutput=isCondition && (node.outputs||[]).some(port=>!state.workflow.edges.some(edge=>edge.source===node.id && (edge.sourceHandle||'result')===port));
  const hasIncoming=state.workflow.edges.some(edge => edge.target === node.id);
  element.querySelector('.node-add').hidden=hasOutgoing && !hasOpenConditionOutput;
  element.querySelector('.connection-origin').hidden=!hasOutgoing;
  element.querySelector('.connection-target').hidden=!hasIncoming;
  const title = element.querySelector('.node-title');
  title.addEventListener('pointerdown', event => dragNode(event, node, element));
  title.querySelector('strong').addEventListener('click', event => { if(node.type==='input'){event.stopPropagation();renameNodePrompt(node.id);} if(node.type==='breakpoint' && node.breakpoint?.cachedOutputs){event.stopPropagation();run();} });
  title.querySelector('strong').addEventListener('dblclick', event => { event.stopPropagation(); if(node.type!=='breakpoint') renameNodePrompt(node.id); });
  const resizeHandle = element.querySelector('.node-resize-handle');
  if (isBreakpoint) element.addEventListener('pointerdown', event => dragNode(event, node, element));
  else {
    resizeHandle.addEventListener('pointerdown', event => resizeNode(event, node, element));
    element.addEventListener('pointermove', event => { const direction=edgeDirection(event, element); element.style.cursor=direction ? `${direction}-resize` : ''; });
    element.addEventListener('pointerdown', event => { const direction=edgeDirection(event, element); if(direction) resizeFromEdge(event, node, element, direction); });
  }
  element.addEventListener('click', event => { if (event.target.classList.contains('handle') || event.target.closest('.node-menu-button')) return; if(state.pendingLinkSource){event.stopPropagation();if(canLinkExisting(nodeById(state.pendingLinkSource),node)) linkExistingNode(state.pendingLinkSource,node.id);return;} select(node.id); });
  element.querySelector('.node-menu-button').onclick = event => { event.stopPropagation(); showNodeMenu(event.currentTarget, node.id); };
  element.querySelector('.node-add').onclick = event => { event.stopPropagation(); showQuickCreate(event.currentTarget, node); };
  return fragment;
}
function portEl(name, type, nodeId) {
  const row = document.createElement('div'); row.className = `port ${type}`; row.textContent = name;
  const handle = document.createElement('span'); handle.className = 'handle'; handle.dataset.node = nodeId; handle.dataset.port = name; handle.dataset.type = type;
  handle.addEventListener('click', event => { event.stopPropagation(); handleLink(event.currentTarget); }); row.append(handle); return row;
}
function render() {
  syncBottomServiceHeight();
  world.querySelectorAll('.node').forEach(node => node.remove());
  state.workflow.nodes.forEach(node => world.append(nodeEl(node)));
  sizeCanvasWorld();
  $('#empty-state').hidden = state.workflow.nodes.length > 0;
  positionStartControls();
  renderWorkstationControls();
  $('#workflow-name').value = state.workflow.name;
  drawLinks(); renderInspector(); renderWorkflows(); syncCodeScroller();
}
function focusAgentInput(nodeId) {
  window.setTimeout(()=>{
    const input=world.querySelector(`.node[data-id="${nodeId}"] .node-input-value`);
    if(!input || input.hidden) return;
    input.focus({preventScroll:true});
    input.setSelectionRange(input.value.length, input.value.length);
  }, 0);
}
function sizeCanvasWorld() { const maxX=Math.max(2200,...state.workflow.nodes.map(node=>(node.x||0)+(node.width||330)+500)); const maxY=Math.max(1100,...state.workflow.nodes.map(node=>(node.y||0)+(node.height||260)+260)); world.style.width=`${maxX}px`; world.style.height=`${maxY}px`; }
function visibleCanvasCenter() { return { x: canvas.scrollLeft + canvas.clientWidth / 2, y: canvas.scrollTop + canvas.clientHeight * 0.45 }; }
function positionStartControls() { const center=visibleCanvasCenter(); const empty=$('#empty-state'); const starter=$('#start-create'); empty.style.left=`${center.x}px`; empty.style.top=`${center.y}px`; starter.style.left=`${center.x}px`; starter.style.top=`${center.y + 78}px`; }
function workstationControlLayer() { let layer=$('#workstation-controls-layer'); if(!layer){layer=document.createElement('div');layer.id='workstation-controls-layer';canvas.parentElement.append(layer);} return layer; }
function workstationPositionFromScreenLeft(left) { return {x:canvas.scrollLeft + left + 10, y:canvas.scrollTop + 78}; }
function workstationButton(label, title, action) { const button=document.createElement('button'); button.type='button'; button.textContent=label; button.title=title; button.setAttribute('aria-label', title); button.onclick=event=>{event.stopPropagation();action(event.currentTarget);}; return button; }
function addBreakpointAt(left) { const node=breakpointNode({x:Math.max(28, canvas.scrollLeft + left + 190), y:0}); state.workflow.nodes.push(node); state.selected=node.id; markDirty(); render(); }
function clearNodeRunState(node) { delete state.runs[node.id]; delete node.lastRun; delete node.lastOutputs; if(node.type==='output') node.displayValue='Waiting for input...'; }
function firstBreakpointNode() { return state.workflow.nodes.filter(node=>node.type==='breakpoint').sort((a,b)=>(a.x||0)-(b.x||0))[0] || null; }
function clearRunStateForMode(mode='whole') {
  if(mode==='whole') { state.runs={}; state.workflow.nodes.forEach(clearNodeRunState); return; }
  const breakpoint=firstBreakpointNode();
  if(!breakpoint) { state.runs={}; state.workflow.nodes.forEach(clearNodeRunState); return; }
  const breakX=Number(breakpoint.x || 0);
  state.workflow.nodes.forEach(node=>{
    const nodeX=Number(node.x || 0);
    const shouldClear=mode==='before-break' ? nodeX <= breakX : nodeX > breakX;
    if(shouldClear) clearNodeRunState(node);
  });
}
function resetWorkstation(mode='whole') {
  const breakpoint=firstBreakpointNode();
  if(!breakpoint || mode==='whole') return resetWorkflowView();
  const breakX=Number(breakpoint.x || 0);
  state.runningNode=null; state.transmittingEdges=new Set(); state.flashNodes=new Set();
  state.workflow.nodes.forEach(node=>{
    const nodeX=Number(node.x || 0);
    const inSection=mode==='before-break' ? nodeX <= breakX : nodeX > breakX;
    if(!inSection) return;
    clearNodeRunState(node);
    if(isDraftAgent(node)) node.code='';
    if(node.type==='breakpoint') node.breakpoint={};
  });
  markDirty();
  render();
}
function renderWorkstationControls() {
  const layer=workstationControlLayer(); layer.replaceChildren();
  const breakpoints=state.workflow.nodes.filter(node=>node.type==='breakpoint').sort((a,b)=>(a.x||0)-(b.x||0));
  const rightControlsLeft=Math.max(12, canvas.clientWidth - 244);
  const starts=breakpoints.length ? [12, rightControlsLeft] : [12];
  [...new Set(starts.map(value=>Math.round(value)))].forEach((left,index)=>{
    if(left > canvas.clientWidth - 54) return;
    const runMode=breakpoints.length ? (index === 0 ? 'before-break' : 'after-break') : 'whole';
    const group=document.createElement('div'); group.className='workstation-controls'; group.style.left=`${left}px`;
    group.append(
      workstationButton('▶', runMode === 'after-break' ? 'Run after workflow break' : runMode === 'before-break' ? 'Run to workflow break' : 'Run workflow', () => run(runMode)),
      workstationButton('↺', runMode === 'after-break' ? 'Reset after break' : runMode === 'before-break' ? 'Reset before break' : 'Reset workflow view', () => resetWorkstation(runMode)),
      workstationButton('💾', 'Save workflow', () => save()),
      workstationButton('Ⅱ', 'Add workflow break', () => addBreakpointAt(left)),
      workstationButton('＋', 'Create node', button => showCanvasCreate(button, workstationPositionFromScreenLeft(left))),
    );
    layer.append(group);
  });
}
function drawLinks() {
  const svg = $('#connections'); svg.replaceChildren(); const bound = world.getBoundingClientRect();
  svg.setAttribute('width', world.offsetWidth); svg.setAttribute('height', world.offsetHeight);
  state.workflow.edges.forEach(edge => {
    const key=edgeKey(edge);
    const a = world.querySelector(`.node[data-id="${edge.source}"] .connection-origin`) || world.querySelector(`.handle[data-node="${edge.source}"][data-port="${edge.sourceHandle}"]`);
    const b = world.querySelector(`.node[data-id="${edge.target}"] .connection-target`) || world.querySelector(`.handle[data-node="${edge.target}"][data-port="${edge.targetHandle}"]`); if (!a || !b) return;
    const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect(); const x1=ar.left+ar.width/2-bound.left, y1=ar.top+ar.height/2-bound.top, x2=br.left+br.width/2-bound.left, y2=br.top+br.height/2-bound.top;
    const path = document.createElementNS('http://www.w3.org/2000/svg','path'); path.dataset.source=edge.source; path.dataset.sourceHandle=edge.sourceHandle || 'result'; path.dataset.target=edge.target; path.dataset.targetHandle=edge.targetHandle || 'input'; path.setAttribute('class',`connection ${state.transmittingEdges.has(key)?'transmitting':''}`); path.setAttribute('d',`M ${x1} ${y1} C ${x1+70} ${y1}, ${x2-70} ${y2}, ${x2} ${y2}`); svg.append(path);
  });
  state.workflow.nodes.filter(isDraftAgent).forEach(node=>{const dock=$(agentBuilder(node)==='codex'?'#codex-dock':'#claudia-dock'), target=world.querySelector(`.node[data-id="${node.id}"]`); if(!dock||!target)return; const ar=dock.getBoundingClientRect(), br=target.getBoundingClientRect(); const x1=ar.left+ar.width/2-bound.left, y1=ar.bottom-bound.top, x2=br.left-bound.left, y2=br.top+br.height/2-bound.top; const path=document.createElementNS('http://www.w3.org/2000/svg','path'); path.setAttribute('class',`connection ${agentBuilder(node)}-draft-link`); path.setAttribute('d',`M ${x1} ${y1} C ${x1} ${y1+80}, ${x2-90} ${y2}, ${x2} ${y2}`); svg.append(path);});
}
function edgeDirection(event, element) { const rect=element.getBoundingClientRect(), edge=8; const north=event.clientY-rect.top<edge, south=rect.bottom-event.clientY<edge, west=event.clientX-rect.left<edge, east=rect.right-event.clientX<edge; if((north||south)&&(west||east)) return `${north?'n':'s'}${west?'w':'e'}`; if(north)return'n';if(south)return's';if(west)return'w';if(east)return'e';return''; }
function dragNode(event,node,element) { if (event.target.closest('button') || edgeDirection(event, element)) return; event.preventDefault(); const bounds=world.getBoundingClientRect(); const x=event.clientX-bounds.left-node.x, y=event.clientY-bounds.top-node.y;
  const move = ev => { node.x=Math.max(0,ev.clientX-bounds.left-x); node.y=node.type==='breakpoint'?0:Math.max(0,ev.clientY-bounds.top-y); element.style.left=`${node.x}px`; element.style.top=`${node.y}px`; drawLinks(); };
  const up = () => { window.removeEventListener('pointermove',move); const inserted=insertNodeIntoNearestEdge(node, element); sizeCanvasWorld(); markDirty(); scheduleLayoutSave(); if(inserted) render(); }; window.addEventListener('pointermove',move); window.addEventListener('pointerup',up,{once:true}); }
function resizeNode(event,node,element) { event.preventDefault(); event.stopPropagation(); const startX=event.clientX,startY=event.clientY,startWidth=element.offsetWidth,startHeight=element.offsetHeight;
  const move=ev=>{node.userSized=true;node.userSizedHeight=true;node.width=Math.max(180,startWidth+ev.clientX-startX);node.height=Math.max(120,startHeight+ev.clientY-startY);element.style.width=`${node.width}px`;element.style.height=`${node.height}px`;drawLinks();};
  const up=()=>{window.removeEventListener('pointermove',move);markDirty();scheduleLayoutSave();};window.addEventListener('pointermove',move);window.addEventListener('pointerup',up,{once:true}); }
function resizeFromEdge(event,node,element,direction) { event.preventDefault(); const startX=event.clientX,startY=event.clientY,startWidth=element.offsetWidth,startHeight=element.offsetHeight,startNodeX=node.x,startNodeY=node.y;
  const move=ev=>{const dx=ev.clientX-startX,dy=ev.clientY-startY;let width=startWidth,height=startHeight,x=startNodeX,y=startNodeY;if(direction.includes('e'))width=Math.max(180,startWidth+dx);if(direction.includes('s'))height=Math.max(120,startHeight+dy);if(direction.includes('w')){width=Math.max(180,startWidth-dx);x=startNodeX+(startWidth-width);}if(direction.includes('n')){height=Math.max(120,startHeight-dy);y=startNodeY+(startHeight-height);}node.userSized=true;node.userSizedHeight=node.userSizedHeight||direction.includes('n')||direction.includes('s');node.width=width;node.height=height;node.x=Math.max(0,x);node.y=Math.max(0,y);element.style.width=`${node.width}px`;if(node.userSizedHeight)element.style.height=`${node.height}px`;element.style.left=`${node.x}px`;element.style.top=`${node.y}px`;drawLinks();};
  const up=()=>{window.removeEventListener('pointermove',move);markDirty();scheduleLayoutSave();};window.addEventListener('pointermove',move);window.addEventListener('pointerup',up,{once:true}); }
function panCanvas(event) { if(event.button!==0 || event.target.closest('.node, button, input, textarea, select, #quick-create, #start-create, #link-node-menu, #unlink-node-menu, #canvas-create, #claudia-menu, #claudia-dock, #codex-menu, #codex-dock, #active-workflow-drawer, #workstation-controls-layer')) return; event.preventDefault(); const startX=event.clientX,startY=event.clientY,startLeft=canvas.scrollLeft,startTop=canvas.scrollTop; canvas.classList.add('panning'); const move=ev=>{canvas.scrollLeft=startLeft-(ev.clientX-startX);canvas.scrollTop=startTop-(ev.clientY-startY);positionStartControls();renderWorkstationControls();drawLinks();}; const up=()=>{window.removeEventListener('pointermove',move);canvas.classList.remove('panning');}; window.addEventListener('pointermove',move); window.addEventListener('pointerup',up,{once:true}); }
function handleLink(handle) { const info={ node:handle.dataset.node, port:handle.dataset.port, type:handle.dataset.type };
  if (!state.linking) { if (info.type === 'output') { state.linking=info; handle.style.background='#8f76ff'; } return; }
  const source=state.linking; state.linking=null; canvas.querySelectorAll('.handle').forEach(h=>h.style.background='');
  if (source.node !== info.node && info.type==='input') { state.workflow.edges = state.workflow.edges.filter(e=>!(e.target===info.node && e.targetHandle===info.port)); state.workflow.edges.push({source:source.node,sourceHandle:source.port,target:info.node,targetHandle:info.port}); drawLinks(); markDirty(); }
}
function hasPath(fromId, toId, visited=new Set()) { if(fromId===toId)return true; if(visited.has(fromId))return false; visited.add(fromId); return state.workflow.edges.filter(edge=>edge.source===fromId).some(edge=>hasPath(edge.target, toId, visited)); }
function canLinkExisting(source, target) { return source && target && source.id!==target.id && (target.inputs||[]).length > 0 && !hasPath(target.id, source.id); }
function nextSourceHandle(source) { const outputs=source?.outputs||['result']; if(source?.type==='condition') return outputs.find(port=>!state.workflow.edges.some(edge=>edge.source===source.id && (edge.sourceHandle||'result')===port)) || outputs[0]; return outputs[0]; }
function startLinkMode(sourceId) { const source=nodeById(sourceId); if(!source)return; state.pendingLinkSource=source.id; $('#quick-create').hidden=true; $('#node-menu').hidden=true; $('#link-node-menu').hidden=true; $('#save-state').textContent='Click a node to link'; render(); }
function clearLinkMode() { state.pendingLinkSource=null; }
function linkExistingNode(sourceId, targetId) { const source=nodeById(sourceId), target=nodeById(targetId); if(!canLinkExisting(source,target)) return; const sourceHandle=nextSourceHandle(source); const targetHandle=preferredTargetHandle(source,target); state.workflow.edges=state.workflow.edges.filter(edge=>!(edge.target===target.id && (edge.targetHandle||'input')===targetHandle)); state.workflow.edges.push({source:source.id,sourceHandle,target:target.id,targetHandle}); clearLinkMode(); $('#link-node-menu').hidden=true; markDirty(); render(); }
function makeNode(kind, position={}) { const number=state.workflow.nodes.length+1; const inputNode=kind==='input'; return {id:id(),name:inputNode?'User input':'Code node',x:position.x ?? 90+number*24,y:position.y ?? 90+number*20,inputs:['input'],outputs:inputNode?['value']:['result'],code:inputNode?'# Set or transform the incoming value\nresult = {"value": inputs.get("input", "")}':"# `inputs` contains values linked into this node\n# Put this node's output in `result`\nresult = {\"result\": inputs.get(\"input\", \"Hello from Python\")}"}; }
function agentNode(position={}) { return {id:id(),type:'agent',name:'Claudia',x:position.x ?? 90,y:position.y ?? 24,inputs:['input'],outputs:['response'],code:`You are Claudia, the local workflow agent for Automated Workflow.

Take the upstream user request and produce a clear work order for the next automation step. Preserve the user's intent, improve specificity, and keep the result practical.`,agent:{model:'local-model',endpoint:'',temperature:0.7}}; }
function agentNameFromPurpose(purpose) { const text=purpose.trim().replace(/\s+/g,' '); if(!text)return 'Specialist Agent'; const words=text.split(' ').slice(0,4).map(word=>word[0]?.toUpperCase()+word.slice(1)); return `${words.join(' ')} Agent`; }
function systemPromptFromPurpose(purpose) { return `You are a specialist workflow agent created by Claudia.

Your role:
${purpose.trim()}

Operating rules:
- Treat the upstream input as the source of truth.
- Preserve the subject, action, setting, mood, and constraints.
- Do not add new subjects, settings, styles, story details, or unrelated ideas.
- Make the smallest useful improvement for this role.
- Keep the output concise and ready for the next workflow node.
- Return only the downstream value, with no explanation.`; }
function specialistAgentNode(purpose, position={}) { const cleanPurpose=(purpose||'Handle the next workflow step clearly and practically.').trim(); return {id:id(),type:'agent',name:agentNameFromPurpose(cleanPurpose),x:position.x ?? 90,y:position.y ?? 24,inputs:['input'],outputs:['response'],code:systemPromptFromPurpose(cleanPurpose),agent:{model:'local-model',endpoint:'',temperature:0.35,purpose:cleanPurpose}}; }
function draftAgentNode(position={}, builder='claudia') { const isCodex=builder==='codex'; return {id:id(),type:'agent',name:isCodex?'New Codex Agent':'New Agent',x:position.x ?? 90,y:position.y ?? 24,inputs:['input'],outputs:['response'],code:'',agent:{model:isCodex?'codex':'local-model',endpoint:'',temperature:0.35,builder,draft:true,claudiaDraft:!isCodex,codexDraft:isCodex}}; }
function outputNode(position={}) { const number=state.workflow.nodes.length+1; return {id:id(),type:'output',name:'Output display',x:position.x ?? 90+number*24,y:position.y ?? 90+number*20,inputs:['input'],outputs:['result'],displayValue:'Waiting for input...'}; }
function killClaudiaNode(position={}) { const number=state.workflow.nodes.length+1; return {id:id(),type:'kill-claudia',name:'Kill Claudia LLM',x:position.x ?? 90+number*24,y:position.y ?? 90+number*20,inputs:['input'],outputs:['result'],code:"pkill -f 'llama-server.*qwen3-vl-30b-q4.gguf'"}; }
function productNode(position={}) { const number=state.workflow.nodes.length+1; const products=state.products?.products || []; const selected=products[0]?.name || ''; return {id:id(),type:'product',name:'Product',x:position.x ?? 90+number*24,y:position.y ?? 90+number*20,inputs:[],outputs:['product'],product:{root:state.products?.root || DEFAULT_PRODUCT_ROOT,products,selected}}; }
async function addProductNode(position={}, source=null) { const node=productNode(position); state.workflow.nodes.push(node); if(source) state.workflow.edges.push({source:source.id,sourceHandle:nextSourceHandle(source),target:node.id,targetHandle:preferredTargetHandle(source,node)}); state.selected=node.id; $('#quick-create').hidden=true; $('#start-create').hidden=true; $('#canvas-create').hidden=true; markDirty(); render(); try{await refreshProducts(node); markDirty(); render();}catch(error){terminalWrite(`Product folders could not be refreshed: ${error.message}\n`);} }
function dataFileNode(position={}) { const number=state.workflow.nodes.length+1; const files=state.dataFiles?.files || []; const selected=files[0]?.path || ''; return {id:id(),type:'file',name:'Data file',x:position.x ?? 90+number*24,y:position.y ?? 90+number*20,inputs:[],outputs:['file'],file:{root:state.dataFiles?.root || DEFAULT_DATA_ROOT,files,selected}}; }
async function addDataFileNode(position={}, source=null) { const node=dataFileNode(position); state.workflow.nodes.push(node); if(source) state.workflow.edges.push({source:source.id,sourceHandle:nextSourceHandle(source),target:node.id,targetHandle:preferredTargetHandle(source,node)}); state.selected=node.id; $('#quick-create').hidden=true; $('#start-create').hidden=true; $('#canvas-create').hidden=true; markDirty(); render(); try{await refreshDataFiles(node); markDirty(); render();}catch(error){terminalWrite(`Data files could not be refreshed: ${error.message}\n`);} }
function addNode() { const node=makeNode('python'); state.workflow.nodes.push(node); state.selected=node.id; markDirty(); render(); }
function starterInput() { const number=state.workflow.nodes.length+1; return {id:id(),type:'input',name:'User input',x:90+number*24,y:90+number*20,inputs:[],outputs:['value'],inputValue:''}; }
function addStarterInput() { const node=starterInput(); state.workflow.nodes.push(node); state.selected=node.id; $('#start-create').hidden=true; closeInspector(); markDirty(); render(); }
function breakpointNode(position={}) { const number=state.workflow.nodes.length+1; return {id:id(),type:'breakpoint',name:'Workflow break',x:position.x ?? 90+number*24,y:0,inputs:['input'],outputs:['image','result'],includeInWorkflow:true,breakpoint:{}}; }
function conditionNode(position={}) { const number=state.workflow.nodes.length+1; return {id:id(),type:'condition',name:'If / Or branch',x:position.x ?? 90+number*24,y:position.y ?? 90+number*20,inputs:['input'],outputs:['if_yes','if_no'],code:'yes',condition:{matchText:'yes',caseSensitive:false}}; }
function taskKind(details) { if(details.taskKind) return details.taskKind; if(details.scriptPath?.endsWith('/file_mailer.py') || details.code?.includes('FileMailerConfig')) return 'file-mailer'; if(details.code?.includes('--work-order')) return 'video-generation'; if(details.code?.includes('/sdapi/v1/txt2img')) return 'image-generation'; return 'python-script'; }
function taskPorts(kind) { if(kind==='video-generation') return {inputs:['prompt','reference_image'],outputs:['video']}; if(kind==='image-generation') return {inputs:['prompt'],outputs:['image']}; if(kind==='file-mailer') return {inputs:['input','product'],outputs:['result']}; return {inputs:['input'],outputs:['result']}; }
function taskNode(details) { const number=state.workflow.nodes.length+1; const scriptName=details.scriptPath.split('/').pop(); const kind=taskKind(details); const ports=taskPorts(kind); return {id:id(),type:'task',name:details.displayName||scriptName,x:90+number*24,y:90+number*20,inputs:ports.inputs,outputs:ports.outputs,code:details.code,task:{scriptPath:details.scriptPath,workingDirectory:details.workingDirectory,pythonExecutable:details.pythonExecutable,usesDetectedVenv:details.usesDetectedVenv,kind}}; }
function updateTaskNodeFromDetails(node, details) { const kind=taskKind(details); const ports=taskPorts(kind); node.code=details.code; node.inputs=ports.inputs; node.outputs=ports.outputs; node.task={...(node.task||{}),scriptPath:details.scriptPath,workingDirectory:details.workingDirectory,pythonExecutable:details.pythonExecutable,usesDetectedVenv:details.usesDetectedVenv,kind}; normalizeTaskPorts(node); }
function scriptPlaceholderNode(source, brief, position={}) { const node=makeNode('python', position); node.name=`${source} script`; node.code=`# ${source} script request\n# ${brief.trim().replaceAll('\n','\n# ')}\n\n# Waiting for generated Python code...`; node.includeInWorkflow=false; return node; }
async function addTask(source=null, options={}) { const button=options.button || $('#menu-task'); const original=button.innerHTML; button.disabled=true; button.textContent=options.loadingText || 'Opening Python script picker…'; try { const details=await request(options.endpoint || '/api/tasks/pick',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}); if(details.cancelled)return; const node=taskNode(details); if(source) { node.x=source.x+320; node.y=source.y; state.workflow.edges.push({source:source.id,sourceHandle:(source.outputs||['result'])[0],target:node.id,targetHandle:preferredTargetHandle(source,node)}); } else if(options.position) { node.x=options.position.x; node.y=options.position.y; } state.workflow.nodes.push(node); state.selected=node.id; $('#quick-create').hidden=true; $('#start-create').hidden=true; $('#canvas-create').hidden=true; markDirty(); render(); } catch(error) { alert(error.message); } finally { button.disabled=false; button.innerHTML=original; } }
function placePopup(menu, left, top, options={}) {
  menu.hidden=false;
  const margin=options.margin ?? 12;
  const width=menu.offsetWidth || options.width || 225;
  const height=Math.min(menu.offsetHeight || options.height || 260, window.innerHeight - margin * 2);
  menu.style.maxHeight=`${window.innerHeight - margin * 2}px`;
  menu.style.overflowY='auto';
  menu.style.left=`${Math.max(margin, Math.min(left, window.innerWidth - width - margin))}px`;
  menu.style.top=`${Math.max(margin, Math.min(top, window.innerHeight - height - margin))}px`;
}
function showQuickCreate(button, source) { const menu=$('#quick-create'); const rect=button.getBoundingClientRect(); menu.dataset.source=source.id; placePopup(menu, rect.right+10, rect.top-10, {height:250}); }
function showNodeMenu(button, nodeId) { const menu=$('#node-menu'); const rect=button.getBoundingClientRect(); const node=nodeById(nodeId); const isTask=node?.type==='task' && node?.task?.scriptPath; menu.hidden=false; menu.dataset.node=nodeId; const buildButton=menu.querySelector('[data-node-action="build-agent"]'); buildButton.hidden=node?.type!=='agent'; buildButton.querySelector('b').textContent=agentBuilder(node)==='codex'?'Build with Codex':'Build with Claudia'; buildButton.querySelector('small').textContent=agentBuilder(node)==='codex'?'Turn the brief into an agent prompt with Codex':'Turn the brief into an agent prompt'; menu.querySelector('[data-node-action="clear-breakpoint"]').hidden=node?.type!=='breakpoint' || !node?.breakpoint?.cachedOutputs; menu.querySelector('[data-node-action="edit-local-llm"]').hidden=!isTask; menu.querySelector('[data-node-action="edit-codex"]').hidden=!isTask; menu.querySelector('[data-node-action="rename"]').hidden=node?.type==='breakpoint'; const includeButton=menu.querySelector('[data-node-action="toggle-include"]'); includeButton.querySelector('b').textContent=node?.includeInWorkflow?'Exclude from workflow':'Include in workflow'; includeButton.querySelector('small').textContent=node?.includeInWorkflow?'Do not run this node unless it is linked':'Run this node even when it is unlinked'; const deleteButton=menu.querySelector('[data-node-action="delete"]'); deleteButton.querySelector('b').textContent=node?.type==='breakpoint'?'Close break':'Delete'; deleteButton.querySelector('small').textContent=node?.type==='breakpoint'?'Remove this workflow break':'Remove this node'; placePopup(menu, rect.right+8, rect.top, {height:340}); }
function standalonePosition() { return {x:canvas.scrollLeft+Math.max(90,canvas.clientWidth-430), y:canvas.scrollTop+70}; }
function showCanvasCreate(anchor=$('#canvas-add'), position=null) { const menu=$('#canvas-create'); const rect=anchor.getBoundingClientRect(); if(position) menu.dataset.position=JSON.stringify(position); else delete menu.dataset.position; placePopup(menu, rect.left, rect.bottom+8, {height:340}); }
function promptForAgentPurpose() { const purpose=prompt('What is this agent supposed to do?','You are an expert image generation prompting agent. Turn short user prompts into detailed image prompts for high quality 8K output.'); return purpose && purpose.trim() ? purpose.trim() : null; }
async function createFreshWorkflow() { $('#canvas-create').hidden=true; if(state.workflow.nodes.length || state.workflow.id){await save();} newWorkflow(); canvas.scrollTo({left:0,top:0,behavior:'auto'}); await loadWorkflows(); }
function createStandaloneNode(kind) { const menu=$('#canvas-create'); const position=menu.dataset.position ? JSON.parse(menu.dataset.position) : standalonePosition(); delete menu.dataset.position; if(kind==='workflow') return createFreshWorkflow().catch(error=>alert(error.message)); if(kind==='file') return addDataFileNode(position).catch(error=>alert(error.message)); if(kind==='product') return addProductNode(position).catch(error=>alert(error.message)); let node;if(kind==='task') return addTask(null,{button:document.querySelector('#canvas-create button[data-create-kind="task"]'),position}); if(kind==='agent') node=draftAgentNode(position); else node=kind==='input'?starterInput():kind==='output'?outputNode(position):kind==='breakpoint'?breakpointNode(position):kind==='kill-claudia'?killClaudiaNode(position):kind==='condition'?conditionNode(position):makeNode('python',position); if(kind==='input'||kind==='breakpoint'){node.x=position.x;node.y=kind==='breakpoint'?0:position.y;} state.workflow.nodes.push(node); state.selected=node.id; $('#canvas-create').hidden=true; markDirty(); render(); }
function showClaudiaMenu() { const menu=$('#claudia-menu'); const rect=$('#claudia-dock').getBoundingClientRect(); placePopup(menu, rect.right+20, rect.top, {height:300}); }
function showCodexMenu() { const menu=$('#codex-menu'); const rect=$('#codex-dock').getBoundingClientRect(); placePopup(menu, rect.right+20, rect.top, {height:300}); }
function spawnSpecialistAgent(source='claudia') { const dock=$(source==='codex'?'#codex-dock':'#claudia-dock').getBoundingClientRect(), bound=world.getBoundingClientRect(); const position={x:Math.max(40,dock.left + dock.width / 2 - bound.left - 180), y:Math.max(90,dock.bottom - bound.top + 58)}; const node=draftAgentNode(position, source); state.workflow.nodes.push(node); state.selected=node.id; $(`#${source}-menu`).hidden=true; markDirty(); render(); focusAgentInput(node.id); }
function deleteCurrentAgent(source='claudia') { const selected=nodeById(state.selected); const agent=(selected?.type==='agent' && selected) || [...state.workflow.nodes].reverse().find(node=>node.type==='agent'); $(`#${source}-menu`).hidden=true; if(!agent){$('#save-state').textContent='No agent node to delete';return;} removeNode(agent.id); }
function sudoPasswordPrompt(action) {
  return new Promise(resolve=>{
    const dialog=$('#sudo-dialog'), form=$('#sudo-form'), input=$('#sudo-password');
    $('#sudo-title').textContent=`Authorize LLM ${action}`;
    $('#sudo-message').textContent=`Enter your sudo password to ${action} the local llama-server service.`;
    input.value='';
    dialog.hidden=false;
    input.focus();
    const cleanup=value=>{form.onsubmit=null;$('#sudo-cancel').onclick=null;dialog.hidden=true;resolve(value);};
    form.onsubmit=event=>{event.preventDefault();cleanup(input.value);};
    $('#sudo-cancel').onclick=()=>cleanup(null);
  });
}
async function requestLlmControl(action, sudoPassword=null) {
  return request(`/api/llm/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(sudoPassword?{sudoPassword}:{})});
}
async function controlLlm(action, source='claudia') { $(`#${source}-menu`).hidden=true; $('#save-state').textContent=action==='start'?'Starting LLM...':'Stopping LLM...'; try{let result=await requestLlmControl(action); if(result.requiresSudo){const sudoPassword=await sudoPasswordPrompt(action); if(!sudoPassword){$('#save-state').textContent=`LLM ${action} cancelled`;return;} result=await requestLlmControl(action,sudoPassword);} $('#save-state').textContent=action==='start'?'LLM started':'LLM stopped'; terminalSection(`LLM ${action}`); terminalWrite(`${result.command || ''}\n${result.output || result.status || ''}\n`); console.log(result);}catch(error){$('#save-state').textContent=`LLM ${action} failed`; alert(error.message);} }
function scriptRequestPrompt(source) {
  return new Promise(resolve=>{
    const dialog=$('#script-request-dialog'), form=$('#script-request-form'), input=$('#script-request-input');
    $('#script-request-title').textContent=`Create ${source} Script`;
    input.value='';
    dialog.hidden=false;
    window.setTimeout(()=>input.focus(), 0);
    const cleanup=value=>{form.onsubmit=null;input.onkeydown=null;$('#script-request-cancel').onclick=null;dialog.hidden=true;resolve(value);};
    form.onsubmit=event=>{event.preventDefault();const value=input.value.trim();if(value)cleanup(value);};
    input.onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();form.requestSubmit();}else if(event.key==='Escape'){event.preventDefault();cleanup(null);}};
    $('#script-request-cancel').onclick=()=>cleanup(null);
  });
}
async function createClaudiaScript() { $('#claudia-menu').hidden=true; const brief=await scriptRequestPrompt('Claudia'); if(!brief) return; const center=visibleCanvasCenter(); const node=scriptPlaceholderNode('Claudia', brief, {x:center.x-120,y:Math.max(90,center.y-80)}); node.name='Claudia building script'; state.workflow.nodes.push(node); state.selected=node.id; state.runningNode=node.id; pendingLocalLlmNodeId=node.id; markDirty(); render(); scrollNodeIntoFocus(node.id); $('#save-state').textContent='Claudia creating script...'; terminalSection('Claudia script project'); terminalWrite('Creating project and asking local LLM...\n'); try{const result=await request('/api/claudia/create-script',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({brief})}); $('#save-state').textContent='Claudia script created'; terminalWrite(`Project: ${result.projectDir}\nEndpoint: ${result.endpoint}\nRun: ${result.runCommand}\n${result.notes?`Notes: ${result.notes}\n`:''}`); if(result.installOutput) terminalWrite(`\nDependencies:\n${result.installOutput}\n`); updateTaskNodeFromDetails(node,result.taskDetails); node.type='task'; node.name=result.taskDetails.displayName || result.projectName || node.task.scriptPath.split('/').pop(); node.includeInWorkflow=true; state.selected=node.id; markDirty(); render();}catch(error){node.name='Claudia script failed'; node.code+=`\n\n# ERROR\n# ${String(error.message).replaceAll('\n','\n# ')}`; $('#save-state').textContent='Claudia script failed'; terminalWrite(`ERROR: ${error.message}\n`); markDirty(); render(); alert(error.message);}finally{if(pendingLocalLlmNodeId===node.id) pendingLocalLlmNodeId=null; if(state.runningNode===node.id){state.runningNode=null;render();}} }
async function createCodexScript() { $('#codex-menu').hidden=true; const brief=await scriptRequestPrompt('Codex'); if(!brief) return; const center=visibleCanvasCenter(); const node=scriptPlaceholderNode('Codex', brief, {x:center.x-120,y:Math.max(90,center.y-80)}); node.name='Codex building script'; state.workflow.nodes.push(node); state.selected=node.id; state.runningNode=node.id; markDirty(); render(); scrollNodeIntoFocus(node.id); $('#save-state').textContent='Creating Codex project...'; terminalSection('Codex script project'); terminalWrite('Preparing Codex script project...\n'); try{const result=await request('/api/codex/create-script',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({brief})}); $('#save-state').textContent='Codex project started'; codexProjectsBySession.set(result.sessionId,{...result,nodeId:node.id}); node.name=`Codex: ${result.projectName}`; node.code+=`\n\n# Project: ${result.projectDir}\n# Codex terminal session is running. This node will refresh when main.py is ready.`; markDirty(); render(); connectTerminalSession(result.sessionId, `Codex project: ${result.projectName}`); terminalWrite(`Project: ${result.projectDir}\nVenv: ${result.pythonExecutable}\n`);}catch(error){node.name='Codex script failed'; node.code+=`\n\n# ERROR\n# ${String(error.message).replaceAll('\n','\n# ')}`; state.runningNode=null; $('#save-state').textContent='Codex project failed'; terminalWrite(`ERROR: ${error.message}\n`); markDirty(); render(); alert(error.message);} }
async function openCodexWorkflowTerminal() { $('#codex-menu').hidden=true; $('#save-state').textContent='Starting Codex workflow terminal...'; terminalSection('Codex workflow terminal'); terminalWrite('Saving workflow context and opening Codex in this repo...\n'); try{if(state.workflow.nodes.length || state.workflow.id) await save(); const result=await request('/api/codex/workflow-terminal',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workflow:state.workflow})}); $('#save-state').textContent='Codex workflow terminal ready'; connectTerminalSession(result.sessionId, `Codex workflow: ${state.workflow.name || 'Untitled workflow'}`); $('#terminal-input').placeholder='Tell Codex what to change in this workflow or dashboard'; terminalWrite(`Project: ${result.projectDir}\nWorkflow: ${result.workflowName || state.workflow.name || 'Untitled workflow'}\n`);}catch(error){$('#save-state').textContent='Codex workflow terminal failed'; terminalWrite(`ERROR: ${error.message}\n`); alert(error.message);} }
function showLinkNodeMenu(sourceId) { const source=nodeById(sourceId); if(!source) return; const menu=$('#link-node-menu'); const quick=$('#quick-create'); const rect=quick.getBoundingClientRect(); const list=$('#link-node-list'); list.replaceChildren(); const candidates=state.workflow.nodes.filter(node=>canLinkExisting(source,node)); if(!candidates.length){const empty=document.createElement('div'); empty.className='link-node-empty'; empty.textContent='No available nodes to link.'; list.append(empty);} candidates.forEach(node=>{const button=document.createElement('button'); button.type='button'; const sourceHandle=(source.outputs||['result'])[0], targetHandle=preferredTargetHandle(source,node); button.innerHTML=`<b></b><small></small>`; button.querySelector('b').textContent=node.name; button.querySelector('small').textContent=`${sourceHandle} → ${targetHandle}`; button.onclick=event=>{event.stopPropagation();linkExistingNode(source.id,node.id);}; list.append(button);}); quick.hidden=true; menu.dataset.source=source.id; placePopup(menu, rect.right+10, rect.top, {height:260}); }
function showUnlinkNodeMenu(nodeId, anchor=null) { const node=nodeById(nodeId); if(!node)return; const menu=$('#unlink-node-menu'); const quick=$('#quick-create'); const rect=(anchor || quick).getBoundingClientRect(); const list=$('#unlink-node-list'); list.replaceChildren(); const links=state.workflow.edges.filter(edge=>edge.source===node.id||edge.target===node.id); if(!links.length){const empty=document.createElement('div'); empty.className='link-node-empty'; empty.textContent='No links on this node.'; list.append(empty);} links.forEach(edge=>{const source=nodeById(edge.source), target=nodeById(edge.target); const button=document.createElement('button'); button.type='button'; button.innerHTML=`<b></b><small></small>`; button.querySelector('b').textContent=`${source?.name||'Missing node'} → ${target?.name||'Missing node'}`; button.querySelector('small').textContent=`${edge.sourceHandle||'result'} → ${edge.targetHandle||'input'}`; button.onclick=event=>{event.stopPropagation();state.workflow.edges=state.workflow.edges.filter(item=>item!==edge);$('#unlink-node-menu').hidden=true;markDirty();render();}; list.append(button);}); quick.hidden=true; $('#node-menu').hidden=true; placePopup(menu, rect.right+10, rect.top, {height:260}); }
function createLinkedNode(kind) { const source=nodeById($('#quick-create').dataset.source); if (!source) return; const position={x:source.x+320,y:kind==='agent'?Math.max(20,source.y-40):source.y}; if(kind==='file') return addDataFileNode(position, source).catch(error=>alert(error.message)); if(kind==='product') return addProductNode(position, source).catch(error=>alert(error.message)); const node=kind==='agent'?draftAgentNode(position):kind==='output'?outputNode(position):kind==='kill-claudia'?killClaudiaNode(position):kind==='condition'?conditionNode(position):makeNode(kind,position); const sourceHandle=nextSourceHandle(source); state.workflow.nodes.push(node); state.workflow.edges.push({source:source.id,sourceHandle,target:node.id,targetHandle:preferredTargetHandle(source,node)}); state.selected=node.id; $('#quick-create').hidden=true; markDirty(); render(); }
function removeNode(nodeId) { state.workflow.nodes=state.workflow.nodes.filter(n=>n.id!==nodeId); state.workflow.edges=state.workflow.edges.filter(e=>e.source!==nodeId&&e.target!==nodeId); if(state.selected===nodeId)state.selected=null; if(state.pendingLinkSource===nodeId)clearLinkMode(); markDirty(); render(); }
function unlinkNode(nodeId) { const before=state.workflow.edges.length; state.workflow.edges=state.workflow.edges.filter(edge=>edge.source!==nodeId && edge.target!==nodeId); $('#node-menu').hidden=true; $('#unlink-node-menu').hidden=true; if(before!==state.workflow.edges.length) markDirty(); render(); }
function toggleIncludeNode(nodeId) { const node=nodeById(nodeId); if(!node)return; node.includeInWorkflow=!node.includeInWorkflow; $('#node-menu').hidden=true; markDirty(); render(); }
function clearBreakpointNode(nodeId) { const node=nodeById(nodeId); if(!node || node.type!=='breakpoint')return; node.breakpoint={}; $('#node-menu').hidden=true; markDirty(); render(); }
function buildDraftAgent(nodeId) { const node=nodeById(nodeId); return agentBuilder(node)==='codex' ? buildAgentWithCodex(nodeId) : buildAgentWithClaudia(nodeId); }
async function buildAgentWithClaudia(nodeId) { const node=nodeById(nodeId); if(!node || node.type!=='agent') return; const brief=(node.code || '').trim(); $('#node-menu').hidden=true; $('#save-state').textContent='Claudia building agent...'; try{const result=await request('/api/agents/build-prompt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({brief})}); node.name=result.name || 'Specialist Agent'; node.code=result.systemPrompt; node.agent={...(node.agent||{}),systemPrompt:result.systemPrompt,purpose:brief,draft:false,claudiaDraft:false,codexDraft:false,builder:'claudia',builtByClaudia:true,buildEndpoint:result.endpoint}; state.workflow.edges=state.workflow.edges.filter(edge=>edge.source!==node.id && edge.target!==node.id); markDirty(); render(); $('#save-state').textContent='Agent built by Claudia';}catch(error){$('#save-state').textContent='Agent build failed'; alert(error.message);} }
async function buildAgentWithCodex(nodeId) { const node=nodeById(nodeId); if(!node || node.type!=='agent') return; const brief=(node.code || '').trim(); $('#node-menu').hidden=true; if(!brief){alert('Type what the agent should do before building it with Codex.');return;} $('#save-state').textContent='Codex building agent...'; terminalWrite('\nPreparing Codex agent build...\n'); try{const result=await request('/api/codex/build-agent',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({brief})}); codexAgentBuildsBySession.set(result.sessionId,{nodeId,brief,projectDir:result.projectDir}); connectTerminalSession(result.sessionId, `Codex agent: ${node.name || 'New Agent'}`); terminalWrite(`Project: ${result.projectDir}\nOutput: ${result.agentPath}\n`);}catch(error){$('#save-state').textContent='Codex agent build failed'; terminalWrite(`ERROR: ${error.message}\n`); alert(error.message);} }
function taskEditPrompt(node, label) { const scriptName=(node.task?.scriptPath || node.name || 'script').split('/').pop(); const instructions=prompt(`${label}: what should change in ${scriptName}?`, 'Fix the bug and keep the existing behavior intact.'); return instructions == null ? null : instructions.trim(); }
async function editTaskWithLocalLlm(nodeId) { const node=nodeById(nodeId); if(!node?.task?.scriptPath)return; const instructions=taskEditPrompt(node,'Local LLM edit'); if(!instructions)return; $('#node-menu').hidden=true; node.code=`# Local LLM edit request\n# Script: ${node.task.scriptPath}\n# ${instructions.replaceAll('\n','\n# ')}\n\n# Waiting for edited Python code...`; state.selected=node.id; state.runningNode=node.id; pendingLocalLlmNodeId=node.id; markDirty(); render(); scrollNodeIntoFocus(node.id); $('#save-state').textContent='Local LLM editing script...'; terminalSection(`Local LLM edit: ${node.name || 'Python script'}`); terminalWrite(`Script: ${node.task.scriptPath}\n`); try{const result=await request('/api/tasks/edit-local-llm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scriptPath:node.task.scriptPath,instructions})}); updateTaskNodeFromDetails(node,result.taskDetails); state.selected=node.id; markDirty(); render(); $('#save-state').textContent='Local LLM edit complete'; terminalWrite(`Endpoint: ${result.endpoint}\n${result.notes?`Notes: ${result.notes}\n`:''}Script updated.\n`);}catch(error){$('#save-state').textContent='Local LLM edit failed'; terminalWrite(`ERROR: ${error.message}\n`); alert(error.message);}finally{if(pendingLocalLlmNodeId===node.id) pendingLocalLlmNodeId=null; if(state.runningNode===node.id){state.runningNode=null;render();}} }
async function editTaskWithCodex(nodeId) { const node=nodeById(nodeId); if(!node?.task?.scriptPath)return; const instructions=taskEditPrompt(node,'Codex edit'); if(!instructions)return; $('#node-menu').hidden=true; $('#save-state').textContent='Starting Codex edit...'; try{const result=await request('/api/tasks/edit-codex',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scriptPath:node.task.scriptPath,instructions})}); codexEditsBySession.set(result.sessionId,{nodeId,scriptPath:result.scriptPath}); $('#save-state').textContent='Codex edit started'; connectTerminalSession(result.sessionId, `Codex edit: ${node.name || 'Python script'}`); terminalWrite(`Script: ${result.scriptPath}\nWorking directory: ${result.workingDirectory}\n`);}catch(error){$('#save-state').textContent='Codex edit failed'; terminalWrite(`ERROR: ${error.message}\n`); alert(error.message);} }
function insertNodeIntoNearestEdge(node, element) { if(!(node.inputs||[]).length || !(node.outputs||[]).length) return false; const rect=element.getBoundingClientRect(); const center={x:rect.left+rect.width/2-world.getBoundingClientRect().left,y:rect.top+rect.height/2-world.getBoundingClientRect().top}; let best=null; world.querySelectorAll('.connection').forEach(path=>{if(path.dataset.source===node.id || path.dataset.target===node.id) return; const length=path.getTotalLength(); for(let distance=0; distance<=length; distance+=Math.max(8,length/36)){const point=path.getPointAtLength(distance); const score=Math.hypot(point.x-center.x, point.y-center.y); if(score<34 && (!best || score<best.score)) best={score,path};}}); if(!best) return false; const edge=state.workflow.edges.find(item=>item.source===best.path.dataset.source && item.target===best.path.dataset.target && (item.sourceHandle||'result')===best.path.dataset.sourceHandle && (item.targetHandle||'input')===best.path.dataset.targetHandle); if(!edge || edge.source===node.id || edge.target===node.id) return false; if(hasPath(edge.target, node.id) || hasPath(node.id, edge.source)) return false; const sourceHandle=edge.sourceHandle || (nodeById(edge.source)?.outputs||['result'])[0]; const targetHandle=edge.targetHandle || (nodeById(edge.target)?.inputs||['input'])[0]; const nodeInput=(node.inputs||['input'])[0]; const nodeOutput=(node.outputs||['result'])[0]; state.workflow.edges=state.workflow.edges.filter(item=>item!==edge); state.workflow.edges.push({source:edge.source,sourceHandle,target:node.id,targetHandle:nodeInput},{source:node.id,sourceHandle:nodeOutput,target:edge.target,targetHandle}); return true; }
function select(nodeId, open=false) { state.selected=nodeId; if(open && nodeById(nodeId)?.type!=='input') openInspector(); render(); }
function openInspector() { $('#app-layout').classList.remove('inspector-closed'); $('#show-inspector').hidden=true; }
function closeInspector() { $('#app-layout').classList.add('inspector-closed'); $('#show-inspector').hidden=false; }
function renderInspector() { const node=nodeById(state.selected); $('#node-form').hidden=!node; $('#inspector-empty').hidden=!!node; if (!node) { $('#run-panel').replaceChildren(); return; }
  const isCondition=node.type==='condition';
  const hideCode=node.type==='input'||node.type==='output'||node.type==='product'||node.type==='file';
  $('.node.selected')?.classList.remove('selected'); world.querySelector(`.node[data-id="${node.id}"]`)?.classList.add('selected'); $('#node-name').value=node.name; $('#node-inputs').value=(node.inputs||[]).join(', '); $('#node-outputs').value=(node.outputs||[]).join(', '); $('#node-code').value=isCondition ? (node.condition?.matchText || node.code || '') : node.code||''; $('#node-code').readOnly=!!node.task || node.type==='input'||node.type==='product'||node.type==='file'; $('#node-code').hidden=hideCode; $('#code-label').hidden=hideCode; $('#code-label').textContent=isCondition?'Match text':node.type==='agent'?'Claudia system prompt':'Python code'; $('#node-inputs-label').hidden=node.type==='input'||node.type==='product'||node.type==='file'; $('#node-outputs-label').hidden=node.type==='input';
  const taskDetails=$('#task-details'); taskDetails.hidden=!node.task; if(node.task) taskDetails.replaceChildren(Object.assign(document.createElement('strong'),{textContent:'PYTHON SCRIPT'}),document.createTextNode(`Script: ${node.task.scriptPath}\nWorking folder: ${node.task.workingDirectory}\nPython: ${node.task.pythonExecutable}`));
  $('#run-panel').replaceChildren(); const run=state.runs[node.id]; if(run) { const div=document.createElement('div'); div.className='run-result'; const label=run.status==='success'?'Last run succeeded':run.status==='skipped'?'Last run skipped':'Last run failed'; div.innerHTML=`<strong>${label}</strong><pre></pre>`; div.querySelector('pre').textContent=run.error || JSON.stringify(run.outputs,null,2) || run.logs; $('#run-panel').append(div); }
}
function bindNodeForm() { [['#node-name','name'],['#node-code','code']].forEach(([selector,key]) => $(selector).addEventListener('input', e=>{const node=nodeById(state.selected); if(node){node[key]=e.target.value;if(node.type==='agent')node.agent={...(node.agent||{}),systemPrompt:e.target.value};if(node.type==='condition')node.condition={...(node.condition||{}),matchText:e.target.value};markDirty(); render();}})); [['#node-inputs','inputs'],['#node-outputs','outputs']].forEach(([selector,key]) => $(selector).addEventListener('change',e=>{const node=nodeById(state.selected);if(node){node[key]=names(e.target.value);markDirty();render();}})); $('#delete-node').onclick=()=>removeNode(state.selected); }
function firstOutputValue(outputs) { if(!outputs || typeof outputs !== 'object') return undefined; if('result' in outputs) return outputs.result; if('input' in outputs) return outputs.input; const values=Object.values(outputs); return values.length ? values[0] : undefined; }
function captureRunOutputsForSave() { state.workflow.nodes.forEach(node=>{const run=state.runs[node.id]; if(!run)return; node.lastRun={status:run.status,outputs:run.outputs||{},error:run.error||'',logs:run.logs||'',savedAt:Date.now()}; node.lastOutputs=run.outputs||{}; if(node.type==='breakpoint' && run.status==='success' && run.outputs && Object.keys(run.outputs).length){node.breakpoint={...(node.breakpoint||{}),cachedAt:Math.floor(Date.now()/1000),cachedOutputs:run.outputs};} if(node.type==='output'){const value=firstOutputValue(run.outputs); if(value!==undefined) node.displayValue=value;} }); }
async function save() {
  if(saveInFlight){saveQueued=true;return savePromise;}
  saveInFlight=true;
  savePromise=(async()=>{
    try {
    do {
      saveQueued=false;
      captureRunOutputsForSave();
      state.workflow.name=$('#workflow-name').value.trim() || 'Untitled workflow';
      const requestRevision=workflowRevision;
      const payload=JSON.parse(JSON.stringify(state.workflow));
      const requestWorkflowId=payload.id || null;
      const url=payload.id?`/api/workflows/${payload.id}`:'/api/workflows';
      const saved=await request(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const workflowStillCurrent=requestWorkflowId ? state.workflow.id===requestWorkflowId : !state.workflow.id;
      if(!workflowStillCurrent){
        await loadWorkflows();
        continue;
      }
      if(requestRevision===workflowRevision){
        state.workflow.id=saved.id;
        state.workflow.updatedAt=saved.updatedAt;
        state.workflow.name=saved.name || state.workflow.name;
        state.workflow.active=Boolean(saved.active);
        state.workflow.favorite=Boolean(saved.favorite);
        state.workflow.recurring=saved.recurring || state.workflow.recurring;
        $('#save-state').textContent=saveQueued?'Unsaved changes':'Saved';
      } else {
        state.workflow.id=state.workflow.id || saved.id;
        state.workflow.updatedAt=saved.updatedAt;
        $('#save-state').textContent='Unsaved changes';
        saveQueued=true;
      }
      await loadWorkflows();
    } while(saveQueued);
    } catch(error){
      alert(error.message);
    } finally {
      saveInFlight=false;
      savePromise=null;
    }
  })();
  return savePromise;
}
async function syncBreakpointCacheFromSaved() { if(!state.workflow.id)return; const saved=await request(`/api/workflows/${state.workflow.id}`); const savedBreakpoints=(saved.nodes||[]).filter(node=>node.type==='breakpoint'); state.workflow.nodes.forEach(node=>{if(node.type!=='breakpoint')return; const savedNode=savedBreakpoints.find(item=>item.id===node.id); if(savedNode?.breakpoint?.cachedOutputs) node.breakpoint=savedNode.breakpoint;}); }
function workflowMenuOpen() { return !!document.querySelector('.workflow-actions:not([hidden])'); }
async function loadWorkflows() { const workflows=await request('/api/workflows'); state.available=workflows; syncWorkflowTerminalVisibility(); if(!workflowMenuOpen()) renderWorkflows(); }
function hasMainTerminalActivity() { return Boolean(state.runningNode) || Boolean($('#run')?.disabled) || Boolean(activeTerminalSession); }
function workflowIsRunning(workflowId) { return activeWorkflowRunStreams.has(workflowId) || workflowTerminalSessions.has(workflowId) || (state.available||[]).some(workflow=>workflow.id===workflowId && workflow.active && workflow.serviceStatus==='running'); }
function syncWorkflowTerminalVisibility() {
  const activeIds=new Set((state.available||[]).filter(workflow=>workflow.active).map(workflow=>workflow.id));
  for(const [workflowId, terminal] of workflowTerminals){
    if(!activeIds.has(workflowId) && !workflowIsRunning(workflowId)) closeWorkflowTerminal(workflowId);
    else scheduleWorkflowTerminalAutoClose(workflowId);
  }
  if(!hasMainTerminalActivity()) scheduleTerminalAutoClose();
}
function closeWorkflowActions() { document.querySelectorAll('.workflow-actions').forEach(menu=>menu.hidden=true); }
function workflowActionButton(label, action, className='') { const button=document.createElement('button'); button.type='button'; button.textContent=label; button.className=className; button.onclick=action; return button; }
function workflowSort(a,b) { if(Boolean(a.favorite)!==Boolean(b.favorite)) return a.favorite ? -1 : 1; return (a.name || '').localeCompare(b.name || '', undefined, {sensitivity:'base'}); }
function activeWorkflowSort(a,b) {
  const aOrder=Number.isFinite(Number(a.activeOrder)) ? Number(a.activeOrder) : Number.MAX_SAFE_INTEGER;
  const bOrder=Number.isFinite(Number(b.activeOrder)) ? Number(b.activeOrder) : Number.MAX_SAFE_INTEGER;
  if(aOrder!==bOrder) return aOrder-bOrder;
  return workflowSort(a,b);
}
function workflowRenameDialog(currentName='Untitled workflow') {
  return new Promise(resolve=>{
    const dialog=$('#rename-workflow-dialog'), form=$('#rename-workflow-form'), input=$('#rename-workflow-input');
    input.value=currentName || 'Untitled workflow';
    dialog.hidden=false;
    input.focus();
    input.select();
    const cleanup=value=>{form.onsubmit=null;$('#rename-workflow-cancel').onclick=null;dialog.hidden=true;resolve(value);};
    form.onsubmit=event=>{event.preventDefault();const name=input.value.trim();cleanup(name || null);};
    $('#rename-workflow-cancel').onclick=()=>cleanup(null);
  });
}
async function renameCurrentWorkflow() { closeWorkflowActions(); const nextName=await workflowRenameDialog(state.workflow.name || 'Untitled workflow'); if(!nextName)return; state.workflow.name=nextName; $('#workflow-name').value=nextName; markDirty(); render(); }
function recurringSeconds(workflow) { return Number(workflow.recurring?.frequencySeconds || 300); }
function countdownText(workflow) { if(workflow.serviceStatus==='running') return 'Running'; const recurring=workflow.recurring||{}; if(recurring.lastStatus==='error') return 'Failed'; if(!recurring.nextRunAt) return 'Waiting'; const remaining=Math.max(0, Math.floor(recurring.nextRunAt - Date.now()/1000)); const minutes=Math.floor(remaining/60), seconds=remaining%60, hours=Math.floor(minutes/60); if(hours) return `${hours}:${String(minutes%60).padStart(2,'0')}:${String(seconds).padStart(2,'0')}`; return `${minutes}:${String(seconds).padStart(2,'0')}`; }
function recurringScheduleLabel(recurring={}) { return recurring.scheduleMode==='dailyTime' ? `daily at ${formatTimeOfDay(recurring.timeOfDay || '09:00')}` : `every ${frequencyLabel(Number(recurring.frequencySeconds || 300))}`; }
function formatTimeOfDay(value) { const [hourText='9', minuteText='00']=String(value || '09:00').split(':'); const hour=Number(hourText), minute=String(minuteText).padStart(2,'0'); const suffix=hour>=12?'PM':'AM'; const displayHour=hour%12 || 12; return `${displayHour}:${minute} ${suffix}`; }
function recurringDaysLabel(weekdays) { const labels=['S','M','T','W','T','F','S']; const days=Array.isArray(weekdays)&&weekdays.length ? weekdays.map(Number).filter(day=>day>=0&&day<=6) : [0,1,2,3,4,5,6]; if(days.length===7)return 'Every day'; return days.map(day=>labels[day]).join(' '); }
function recurringDetailLabel(recurring={}) { return `${recurringScheduleLabel(recurring)} · ${recurringDaysLabel(recurring.weekdays)}`; }
function countdownParts(workflow) {
  if(workflow.serviceStatus==='running') return {status:'Running now'};
  const recurring=workflow.recurring||{};
  if(recurring.lastStatus==='error') return {status:'Last run failed'};
  if(!recurring.nextRunAt) return {status:'Waiting'};
  let remaining=Math.max(0, Math.floor(recurring.nextRunAt - Date.now()/1000));
  const days=Math.floor(remaining/86400); remaining-=days*86400;
  const hours=Math.floor(remaining/3600); remaining-=hours*3600;
  const minutes=Math.floor(remaining/60);
  const seconds=remaining-minutes*60;
  return {days,hours,minutes,seconds};
}
function countdownLabel(workflow) {
  const parts=countdownParts(workflow);
  if(parts.status) return parts.status;
  const units=[];
  if(parts.days) units.push(`${parts.days}d`);
  if(parts.hours || units.length) units.push(`${parts.hours}h`);
  units.push(`${parts.minutes}m`);
  units.push(`${parts.seconds}s`);
  return `Next in ${units.join(' ')}`;
}
function countdownTile(value, label) { const tile=document.createElement('span'); tile.className='countdown-tile'; const number=document.createElement('b'); number.textContent=String(value).padStart(2,'0'); const caption=document.createElement('small'); caption.textContent=label; tile.append(number,caption); return tile; }
function flipCountdown(workflow) {
  const wrap=document.createElement('span');
  wrap.className='workflow-flip-countdown';
  wrap.textContent=countdownLabel(workflow);
  return wrap;
}
function workflowRow(workflow, location) {
  const recurring=workflow.recurring||{};
  const row=document.createElement('div');
  row.className=`workflow-row workflow-row-${location} ${workflow.id===state.workflow.id?'active':''} ${workflow.serviceStatus==='running'?'running-service':''} ${workflow.serviceStatus==='waiting'?'waiting-service':''} ${recurring.lastStatus==='error'?'error-service':''}`;
  row.draggable=true;
  row.dataset.workflowId=workflow.id;
  row.dataset.workflowLocation=location;
  row.addEventListener('dragstart', event=>startWorkflowDrag(event, workflow, location));
  row.addEventListener('dragend', endWorkflowDrag);
  if(location==='active'){
    row.addEventListener('dragover', activeWorkflowDragOver);
    row.addEventListener('drop', event=>dropWorkflowOnActiveList(event, workflow.id));
  }
  const button=document.createElement('button');
  button.className='workflow-item';
  const name=document.createElement('span');
  name.className='workflow-item-name';
  name.textContent=workflow.name;
  button.append(name);
  if(location==='active' && recurring.enabled){
    const meta=document.createElement('span');
    meta.className='workflow-active-meta';
    meta.textContent=recurringDetailLabel(recurring);
    button.append(meta, flipCountdown(workflow));
  }
  if(location!=='active'){
    const count=document.createElement('span');
    count.className='workflow-node-count';
    count.textContent=`${workflow.nodeCount ?? 0} ${workflow.nodeCount === 1 ? 'node' : 'nodes'}`;
    button.append(count);
  }
  button.onclick=()=>{clearTimeout(workflowClickTimer);workflowClickTimer=window.setTimeout(()=>load(workflow.id),180);};
  button.ondblclick=event=>{event.preventDefault();event.stopPropagation();clearTimeout(workflowClickTimer);renameWorkflow(workflow);};
  const menuButton=document.createElement('button');
  menuButton.type='button';
  menuButton.className='workflow-menu-button';
  menuButton.title='Workflow actions';
  menuButton.textContent='⋮';
  const menu=document.createElement('div');
  menu.className='workflow-actions';
  menu.hidden=true;
  menu.onclick=event=>event.stopPropagation();
  const menuItems=[
    workflowActionButton('Run now', () => runWorkflowNow(workflow.id)),
    workflowActionButton('Rename', () => renameWorkflow(workflow)),
    workflowActionButton('Copy', () => copyWorkflow(workflow.id)),
    workflowActionButton('Paste copied workflow', () => pasteWorkflow()),
  ];
  if(location==='history') {
    menuItems.splice(1,0, workflowActionButton('Move to Active Workflows', () => setWorkflowActive(workflow.id, true)));
  }
  if(location==='active') {
    const activeActions=[
      workflowActionButton(recurring.enabled?'Recurring settings':'Make Recurring', () => configureRecurringWorkflow(workflow)),
    ];
    if(recurring.enabled) activeActions.push(workflowActionButton('Disable recurring', () => toggleWorkflowService(workflow)));
    activeActions.push(workflowActionButton('Remove from Active Workflows', () => setWorkflowActive(workflow.id, false)));
    menuItems.splice(1,0,...activeActions);
  }
  menuItems.push(workflowActionButton('Delete workflow', () => deleteWorkflow(workflow.id), 'danger-action'));
  menu.append(...menuItems);
  menuButton.onclick=event=>{event.preventDefault();event.stopPropagation();const wasHidden=menu.hidden;closeWorkflowActions();if(wasHidden){const rect=menuButton.getBoundingClientRect();placePopup(menu, rect.right-190, rect.bottom+6, {width:190,height:300});}};
  if(location==='history'){
    const star=document.createElement('button');
    star.type='button';
    star.className=`workflow-star ${workflow.favorite?'favorited':''}`;
    star.title=workflow.favorite?'Remove favorite':'Add favorite';
    star.textContent=workflow.favorite?'★':'☆';
    star.onclick=event=>{event.stopPropagation();toggleWorkflowFavorite(workflow);};
    row.append(star);
  }
  row.append(button);
  if(location==='active'){
    const displayButton=document.createElement('button');
    displayButton.type='button';
    displayButton.className=`workflow-service-button display ${recurring.displayMode==='silent'?'silent':'verbose'}`;
    displayButton.textContent=recurring.displayMode==='silent'?'Silent':'Verbose';
    displayButton.title=recurring.displayMode==='silent'?'Do not open the recurring terminal':'Open the recurring terminal';
    displayButton.onclick=event=>{event.stopPropagation();toggleWorkflowDisplayMode(workflow);};
    row.append(displayButton);
    if(recurring.enabled){
      const editButton=document.createElement('button');
      editButton.type='button';
      editButton.className='workflow-service-button edit';
      editButton.textContent='Edit';
      editButton.title='Edit recurring schedule';
      editButton.onclick=event=>{event.stopPropagation();configureRecurringWorkflow(workflow);};
      row.append(editButton);
    }
    const serviceButton=document.createElement('button');
    serviceButton.type='button';
    serviceButton.className=`workflow-service-button ${recurring.enabled?'stop':'start'}`;
    serviceButton.textContent=recurring.enabled?'Stop':'Start';
    serviceButton.onclick=event=>{event.stopPropagation();recurring.enabled ? toggleWorkflowService(workflow) : configureRecurringWorkflow(workflow);};
    row.append(serviceButton);
  }
  row.append(menuButton,menu);
  return row;
}
function currentWorkflowDraftRow() {
  const row=document.createElement('div');
  row.className='workflow-row workflow-row-draft active';
  const button=document.createElement('button');
  button.className='workflow-item';
  const name=document.createElement('span');
  name.className='workflow-item-name';
  name.textContent=state.workflow.name || 'Untitled workflow';
  const count=document.createElement('span');
  count.className='workflow-node-count';
  count.textContent=`Draft · ${state.workflow.nodes.length} ${state.workflow.nodes.length === 1 ? 'node' : 'nodes'}`;
  button.append(name,count);
  button.ondblclick=event=>{event.preventDefault();event.stopPropagation();renameCurrentWorkflow();};
  const menuButton=document.createElement('button');
  menuButton.type='button';
  menuButton.className='workflow-menu-button';
  menuButton.title='Workflow actions';
  menuButton.textContent='⋮';
  const menu=document.createElement('div');
  menu.className='workflow-actions';
  menu.hidden=true;
  menu.onclick=event=>event.stopPropagation();
  menu.append(
    workflowActionButton('Rename', () => renameCurrentWorkflow()),
    workflowActionButton('Save workflow', () => {closeWorkflowActions();save();}),
  );
  menuButton.onclick=event=>{event.preventDefault();event.stopPropagation();const wasHidden=menu.hidden;closeWorkflowActions();if(wasHidden){const rect=menuButton.getBoundingClientRect();placePopup(menu, rect.right-190, rect.bottom+6, {width:190,height:180});}};
  row.append(button,menuButton,menu);
  return row;
}
function activeWorkflowIdsFromDom() { return [...$('#active-workflow-list').querySelectorAll('.workflow-row-active')].map(row=>row.dataset.workflowId).filter(Boolean); }
function activeWorkflowDropIndex(event) {
  const rows=[...$('#active-workflow-list').querySelectorAll('.workflow-row-active:not(.dragging)')];
  for(const row of rows){
    const rect=row.getBoundingClientRect();
    if(event.clientY < rect.top + rect.height / 2) return rows.indexOf(row);
  }
  return rows.length;
}
function activeWorkflowIdsWithDrop(workflowId, index) {
  const ids=activeWorkflowIdsFromDom().filter(id=>id!==workflowId);
  ids.splice(Math.max(0, Math.min(index, ids.length)), 0, workflowId);
  return ids;
}
function startWorkflowDrag(event, workflow, location) {
  if(event.target.closest('.workflow-menu-button,.workflow-star,.workflow-service-button,.workflow-actions')) {
    event.preventDefault();
    return;
  }
  workflowDrag={id:workflow.id, location};
  event.currentTarget.classList.add('dragging');
  event.dataTransfer.effectAllowed='move';
  event.dataTransfer.setData('text/plain', workflow.id);
  $('#active-workflow-list').classList.add('workflow-drop-ready');
  if(!$('#bottom-service-panel').classList.contains('collapsed')) return;
  $('#bottom-service-panel').classList.remove('collapsed');
  $('#active-workflow-toggle').setAttribute('aria-expanded','true');
}
function endWorkflowDrag() {
  document.querySelectorAll('.workflow-row.dragging,.workflow-row.drop-before,.workflow-row.drop-after').forEach(row=>row.classList.remove('dragging','drop-before','drop-after'));
  $('#active-workflow-list').classList.remove('workflow-drop-ready');
  workflowDrag=null;
}
function activeWorkflowDragOver(event) {
  if(!workflowDrag) return;
  event.preventDefault();
  event.dataTransfer.dropEffect='move';
  document.querySelectorAll('.workflow-row.drop-before,.workflow-row.drop-after').forEach(row=>row.classList.remove('drop-before','drop-after'));
  const rect=event.currentTarget.getBoundingClientRect();
  event.currentTarget.classList.add(event.clientY < rect.top + rect.height / 2 ? 'drop-before' : 'drop-after');
}
function activeWorkflowListDragOver(event) {
  if(!workflowDrag) return;
  event.preventDefault();
  event.dataTransfer.dropEffect='move';
}
async function dropWorkflowOnActiveList(event, targetWorkflowId=null) {
  if(!workflowDrag) return;
  event.preventDefault();
  const targetRow=targetWorkflowId ? $(`.workflow-row-active[data-workflow-id="${CSS.escape(targetWorkflowId)}"]`) : null;
  let index=targetRow ? activeWorkflowIdsFromDom().filter(id=>id!==workflowDrag.id).indexOf(targetWorkflowId) : activeWorkflowDropIndex(event);
  if(targetRow){
    const rect=targetRow.getBoundingClientRect();
    if(event.clientY >= rect.top + rect.height / 2) index += 1;
  }
  const workflowId=workflowDrag.id;
  const dragLocation=workflowDrag.location;
  const order=activeWorkflowIdsWithDrop(workflowId, index);
  endWorkflowDrag();
  try{
    await request('/api/workflows/active-order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workflowIds:order})});
    await loadWorkflows();
    if(dragLocation==='history') $('#save-state').textContent='Added to Active Workflows';
  }catch(error){alert(error.message);}
}
function renderWorkflows() { const workflows=[...(state.available||[])].sort(workflowSort); const active=[...(state.available||[])].filter(workflow=>workflow.active).sort(activeWorkflowSort); const historyList=$('#workflow-list'); const activeList=$('#active-workflow-list'); historyList.replaceChildren(); activeList.replaceChildren(); activeList.ondragover=activeWorkflowListDragOver; activeList.ondrop=dropWorkflowOnActiveList; $('#workflow-count').textContent=workflows.length + (state.workflow.id ? 0 : 1); $('#active-workflow-count').textContent=active.length; if(!state.workflow.id) historyList.append(currentWorkflowDraftRow()); workflows.forEach(workflow=>historyList.append(workflowRow(workflow, 'history'))); if(active.length){active.forEach(workflow=>activeList.append(workflowRow(workflow, 'active')));} else {const empty=document.createElement('div');empty.className='workflow-empty-row';empty.textContent='Drop a workflow here to activate it';activeList.append(empty);} }
async function load(workflowId) { clearTimeout(layoutSaveTimer); saveQueued=false; state.workflow=await request(`/api/workflows/${workflowId}`); workflowRevision=0; state.selected=null; state.runs={}; clearLinkMode(); clearRunVisuals(); $('#save-state').textContent='Saved'; render(); }
async function renameWorkflow(workflow) { closeWorkflowActions(); const nextName=await workflowRenameDialog(workflow.name || 'Untitled workflow'); if(!nextName) return; try{const full=await request(`/api/workflows/${workflow.id}`); full.name=nextName; await request(`/api/workflows/${workflow.id}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(full)}); if(state.workflow.id===workflow.id) state.workflow.name=nextName; await loadWorkflows(); render();}catch(error){alert(error.message);} }
async function copyWorkflow(workflowId) { closeWorkflowActions(); try{state.copiedWorkflow=await request(`/api/workflows/${workflowId}`); $('#save-state').textContent='Workflow copied';}catch(error){alert(error.message);} }
async function pasteWorkflow() { closeWorkflowActions(); if(!state.copiedWorkflow){alert('Copy a workflow first.');return;} const copy=typeof structuredClone === 'function' ? structuredClone(state.copiedWorkflow) : JSON.parse(JSON.stringify(state.copiedWorkflow)); copy.id=null; copy.active=false; delete copy.favorite; copy.recurring={enabled:false,frequencySeconds:300}; copy.name=`${copy.name || 'Untitled workflow'} copy`; delete copy.updatedAt; try{clearTimeout(layoutSaveTimer);saveQueued=false;state.workflow=await request('/api/workflows',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(copy)}); workflowRevision=0; state.selected=null; state.runs={}; $('#save-state').textContent='Saved'; await loadWorkflows(); render();}catch(error){alert(error.message);} }
async function setWorkflowActive(workflowId, active) { closeWorkflowActions(); try{const workflow=await request(`/api/workflows/${workflowId}`); workflow.active=active; if(active && !Number.isFinite(Number(workflow.activeOrder))){const activeOrders=(state.available||[]).filter(item=>item.active).map(item=>Number(item.activeOrder)).filter(Number.isFinite); workflow.activeOrder=activeOrders.length ? Math.max(...activeOrders)+1 : 0;} await request(`/api/workflows/${workflowId}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(workflow)}); if(state.workflow.id===workflowId){state.workflow.active=active; state.workflow.activeOrder=workflow.activeOrder;} await loadWorkflows(); if(active) $('#save-state').textContent='Added to Active Workflows'; else closeWorkflowTerminal(workflowId);}catch(error){alert(error.message);} }
async function toggleWorkflowFavorite(workflow) { closeWorkflowActions(); try{const full=await request(`/api/workflows/${workflow.id}`); full.favorite=!workflow.favorite; await request(`/api/workflows/${workflow.id}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(full)}); if(state.workflow.id===workflow.id) state.workflow.favorite=full.favorite; await loadWorkflows();}catch(error){alert(error.message);} }
async function toggleWorkflowDisplayMode(workflow) { closeWorkflowActions(); const next=workflow.recurring?.displayMode==='silent'?'verbose':'silent'; try{const saved=await request(`/api/workflows/${workflow.id}/recurring`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({displayMode:next})}); if(state.workflow.id===workflow.id){state.workflow.recurring=saved.recurring;state.workflow.updatedAt=saved.updatedAt;} await loadWorkflows();}catch(error){alert(error.message);} }
function frequencyLabel(seconds) { if(seconds<60)return `${seconds} sec`; if(seconds===60)return '1 min'; if(seconds===300)return '5 min'; if(seconds===1800)return '30 min'; if(seconds===3600)return '1 hr'; if(seconds>=86400)return '1 day'; if(seconds>=3600)return `${Math.round(seconds/3600)} hr`; return `${Math.round(seconds/60)} min`; }
function recurringSettingsDialog(workflow) {
  return new Promise(resolve=>{
    const recurring=workflow.recurring||{}, dialog=$('#recurring-dialog'), form=$('#recurring-form'), schedule=$('#recurring-schedule'), timeInput=$('#recurring-time'), timeLabel=$('#recurring-time-label');
    const dayInputs=[...dialog.querySelectorAll('.recurring-days input')];
    schedule.value=recurring.scheduleMode==='dailyTime'?'dailyTime':String([5,15,30,60,300,1800,3600].includes(recurringSeconds(workflow))?recurringSeconds(workflow):3600);
    timeInput.value=recurring.timeOfDay || '09:00';
    const weekdays=Array.isArray(recurring.weekdays)&&recurring.weekdays.length ? recurring.weekdays.map(Number) : [0,1,2,3,4,5,6];
    dayInputs.forEach(input=>{input.checked=weekdays.includes(Number(input.value));});
    const sync=()=>{timeLabel.hidden=schedule.value!=='dailyTime';};
    sync();
    dialog.hidden=false;
    schedule.focus();
    const cleanup=value=>{form.onsubmit=null;$('#recurring-cancel').onclick=null;schedule.onchange=null;dialog.hidden=true;resolve(value);};
    schedule.onchange=sync;
    form.onsubmit=event=>{event.preventDefault(); const selectedDays=dayInputs.filter(input=>input.checked).map(input=>Number(input.value)); const weekdays=selectedDays.length?selectedDays:[0,1,2,3,4,5,6]; if(schedule.value==='dailyTime') cleanup({enabled:true,scheduleMode:'dailyTime',timeOfDay:timeInput.value || '09:00',frequencySeconds:recurringSeconds(workflow),weekdays}); else cleanup({enabled:true,scheduleMode:'interval',frequencySeconds:Number(schedule.value),weekdays});};
    $('#recurring-cancel').onclick=()=>cleanup(null);
  });
}
async function configureRecurringWorkflow(workflow) { closeWorkflowActions(); const settings=await recurringSettingsDialog(workflow); if(!settings)return; try{const saved=await request(`/api/workflows/${workflow.id}/recurring`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(settings)}); if(state.workflow.id===workflow.id){state.workflow.recurring=saved.recurring;state.workflow.updatedAt=saved.updatedAt;} $('#save-state').textContent=`Recurring enabled: ${settings.scheduleMode==='dailyTime' ? `daily at ${formatTimeOfDay(settings.timeOfDay)}` : `every ${frequencyLabel(settings.frequencySeconds)}`}`; await loadWorkflows();}catch(error){alert(error.message);} }
async function toggleWorkflowService(workflow) { const action=workflow.recurring?.enabled?'stop':'start'; try{const service=await request(`/api/workflows/${workflow.id}/service/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({frequencySeconds:recurringSeconds(workflow)})}); if(state.workflow.id===workflow.id){state.workflow.recurring={...(state.workflow.recurring||{}),enabled:action==='start',nextRunAt:service.nextRunAt??null};} $('#save-state').textContent=`Recurring workflow ${action==='start'?'started':'stopped'}`; await loadWorkflows();}catch(error){alert(error.message);} }
function workflowRunError(run) { const failed=run?.nodes?.find(node=>node.status==='error'); if(!failed) return null; const detail=String(failed.error || failed.logs || 'A workflow node failed.').trim(); return detail.split(/\r?\n/).filter(line=>line.trim()).pop() || 'A workflow node failed.'; }
async function runWorkflowNow(workflowId) { closeWorkflowActions(); try{const workflow=await request(`/api/workflows/${workflowId}`); const title=workflow.name || 'Workflow'; $('#save-state').textContent=`Running ${title}...`; const data=await request(`/api/workflows/${workflowId}/run`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:'whole'})}); activeWorkflowRunStreams.add(workflowId); workflowRunIds.set(workflowId,data.runId); const finishStream=status=>{activeWorkflowRunStreams.delete(workflowId);workflowRunIds.delete(workflowId);workflowRunEventSources.delete(workflowId);$('#save-state').textContent=status;loadWorkflows().catch(console.warn);}; const stream=new EventSource(`/api/runs/${data.runId}/events`);workflowRunEventSources.set(workflowId,stream);stream.onmessage=event=>{const message=JSON.parse(event.data); if(message.type==='complete'){const failure=workflowRunError(message.run);stream.close();finishStream(`${title} ${failure?'failed':'finished'}`);if(failure) alert(failure);} else if(message.type==='error'){stream.close();finishStream(`${title} failed`);alert(message.text);}};stream.onerror=()=>{if(stream.readyState===EventSource.CLOSED) finishStream(`${title} stopped`);};}catch(error){alert(error.message);} }
async function deleteWorkflow(workflowId) { closeWorkflowActions(); try{await request(`/api/workflows/${workflowId}`,{method:'DELETE'}); if(state.workflow.id===workflowId) newWorkflow(); await loadWorkflows();}catch(error){alert(error.message);} }
function scheduleTerminalAutoClose() {
  if($('#terminal').hidden || hasMainTerminalActivity() || activeTerminalSession || terminalAutoCloseTimer) return;
  terminalAutoCloseTimer=window.setTimeout(()=>{
    terminalAutoCloseTimer=null;
    if(!hasMainTerminalActivity() && !activeTerminalSession) $('#terminal').hidden=true;
  },30000);
}
function openTerminal() { clearTimeout(terminalAutoCloseTimer); terminalAutoCloseTimer=null; $('#terminal').hidden=false; if(!hasMainTerminalActivity() && !activeTerminalSession) scheduleTerminalAutoClose(); }
function clearTerminal() { $('#terminal-output').textContent=''; }
function cleanTerminalText(text) { return String(text).replace(/\x1b\][^\x07]*(?:\x07|\x1b\\)/g,'').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g,'').replace(/\x1b[()][A-Za-z0-9]/g,'').replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g,''); }
function terminalWrite(text) { openTerminal(); const output=$('#terminal-output'); output.textContent+=cleanTerminalText(text); output.scrollTop=output.scrollHeight; }
function terminalSection(title) { terminalWrite(`\n--- ${title} ---\n`); }
function attachWorkflowTerminalSession(sessionId) { activeTerminalSession=sessionId; openTerminal(); $('#terminal-input').placeholder='Send input to running workflow'; }
function workflowTerminalTitle(workflowId, fallback='Workflow') {
  const workflow=(state.available||[]).find(item=>item.id===workflowId);
  return workflow?.name || fallback || workflowId;
}
function workflowTerminalPosition() {
  let index=0;
  for(const terminal of workflowTerminals.values()){
    terminal.element.style.setProperty('--workflow-terminal-index', index);
    index+=1;
  }
}
function ensureWorkflowTerminal(workflowId, title='Workflow') {
  const existing=workflowTerminals.get(workflowId);
  if(existing){
    existing.title.textContent=`${workflowTerminalTitle(workflowId,title)} WORKFLOW TERMINAL`;
    existing.element.hidden=false;
    clearTimeout(existing.closeTimer);
    workflowTerminalPosition();
    return existing;
  }
  const element=document.createElement('section');
  element.className='workflow-terminal';
  element.dataset.workflowId=workflowId;
  const head=document.createElement('div');
  head.className='workflow-terminal-head';
  const titleEl=document.createElement('span');
  titleEl.innerHTML='<i></i><b></b>';
  titleEl.querySelector('b').textContent=`${workflowTerminalTitle(workflowId,title)} WORKFLOW TERMINAL`;
  const close=document.createElement('button');
  close.type='button';
  close.textContent='x';
  close.title='Close workflow terminal';
  close.onclick=()=>closeWorkflowTerminal(workflowId);
  head.append(titleEl,close);
  const output=document.createElement('pre');
  output.className='workflow-terminal-output';
  const form=document.createElement('form');
  form.className='workflow-terminal-input-form';
  const prompt=document.createElement('span');
  prompt.textContent='>';
  const input=document.createElement('input');
  input.autocomplete='off';
  input.spellcheck=false;
  input.placeholder='Send input to this workflow session';
  form.append(prompt,input);
  form.addEventListener('submit', event=>{
    event.preventDefault();
    const text=input.value;
    input.value='';
    sendWorkflowTerminalInput(workflowId,text).catch(error=>workflowTerminalWrite(workflowId,`ERROR: ${error.message}\n`,title));
  });
  element.append(head,output,form);
  document.body.append(element);
  const terminal={element,title:titleEl.querySelector('b'),output,input,closeTimer:null};
  workflowTerminals.set(workflowId,terminal);
  workflowTerminalPosition();
  return terminal;
}
function scheduleWorkflowTerminalAutoClose(workflowId) {
  const terminal=workflowTerminals.get(workflowId);
  if(!terminal || terminal.element.hidden || workflowIsRunning(workflowId)) return;
  if(terminal.closeTimer) return;
  terminal.closeTimer=window.setTimeout(()=>{
    terminal.closeTimer=null;
    if(!workflowIsRunning(workflowId)) closeWorkflowTerminal(workflowId);
  },WORKFLOW_TERMINAL_IDLE_MS);
}
function cancelWorkflowTerminalAutoClose(workflowId) {
  const terminal=workflowTerminals.get(workflowId);
  if(!terminal)return;
  clearTimeout(terminal.closeTimer);
  terminal.closeTimer=null;
}
function finishWorkflowTerminalRun(workflowId) {
  activeWorkflowRunStreams.delete(workflowId);
  recurringWorkflowTerminalRuns.delete(workflowId);
  workflowRunIds.delete(workflowId);
  const runSource=workflowRunEventSources.get(workflowId);
  if(runSource){runSource.close();workflowRunEventSources.delete(workflowId);}
  const terminalSource=workflowTerminalEventSources.get(workflowId);
  if(terminalSource){terminalSource.close();workflowTerminalEventSources.delete(workflowId);}
  workflowTerminalSessions.delete(workflowId);
  const workflow=(state.available||[]).find(item=>item.id===workflowId);
  if(workflow && workflow.serviceStatus==='running') workflow.serviceStatus='waiting';
  scheduleWorkflowTerminalAutoClose(workflowId);
}
async function stopWorkflowTerminalRun(workflowId) {
  const runSource=workflowRunEventSources.get(workflowId);
  if(runSource){runSource.close();workflowRunEventSources.delete(workflowId);}
  const runId=workflowRunIds.get(workflowId);
  workflowRunIds.delete(workflowId);
  activeWorkflowRunStreams.delete(workflowId);
  recurringWorkflowTerminalRuns.delete(workflowId);
  if(runId){try{await request(`/api/runs/${runId}/stop`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}catch(error){console.warn(error);}}
  const sessionId=workflowTerminalSessions.get(workflowId);
  workflowTerminalSessions.delete(workflowId);
  if(sessionId){try{await request(`/api/terminal/${sessionId}/stop`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}catch(error){console.warn(error);}}
}
async function closeWorkflowTerminal(workflowId) {
  const terminal=workflowTerminals.get(workflowId);
  if(!terminal)return;
  clearTimeout(terminal.closeTimer);
  if(workflowRunIds.has(workflowId) || workflowTerminalSessions.has(workflowId) || activeWorkflowRunStreams.has(workflowId)) await stopWorkflowTerminalRun(workflowId);
  const source=workflowTerminalEventSources.get(workflowId);
  if(source){source.close();workflowTerminalEventSources.delete(workflowId);}
  workflowTerminals.delete(workflowId);
  recurringWorkflowTerminalRuns.delete(workflowId);
  terminal.element.remove();
  workflowTerminalPosition();
}
function workflowTerminalWrite(workflowId, text, title='Workflow') {
  const terminal=ensureWorkflowTerminal(workflowId,title);
  terminal.output.textContent+=cleanTerminalText(text);
  terminal.output.scrollTop=terminal.output.scrollHeight;
  scheduleWorkflowTerminalAutoClose(workflowId);
}
function workflowTerminalSection(workflowId, title) { workflowTerminalWrite(workflowId,`\n--- ${title} ---\n`,title); }
function attachWorkflowServiceTerminalSession(workflowId, sessionId, title='Workflow') {
  const existing=workflowTerminalEventSources.get(workflowId);
  if(existing) existing.close();
  cancelWorkflowTerminalAutoClose(workflowId);
  workflowTerminalSessions.set(workflowId,sessionId);
  workflowTerminalWrite(workflowId,'Interactive workflow terminal attached.\n',title);
  const source=new EventSource(`/api/terminal/${sessionId}/events`);
  workflowTerminalEventSources.set(workflowId,source);
  source.onmessage=event=>{
    const message=JSON.parse(event.data);
    if(message.type==='start') workflowTerminalWrite(workflowId,`Session ${message.kind} started in ${message.cwd}\n`,title);
    else if(message.type==='output') workflowTerminalWrite(workflowId,message.text,title);
    else if(message.type==='exit'){
      workflowTerminalWrite(workflowId,`\nProcess exited${message.code==null?'':` with code ${message.code}`}.\n`,title);
      source.close();
      workflowTerminalEventSources.delete(workflowId);
      workflowTerminalSessions.delete(workflowId);
      scheduleWorkflowTerminalAutoClose(workflowId);
    } else if(message.type==='error') workflowTerminalWrite(workflowId,`\nERROR: ${message.text}\n`,title);
  };
  source.onerror=()=>{source.close();workflowTerminalEventSources.delete(workflowId);workflowTerminalSessions.delete(workflowId);scheduleWorkflowTerminalAutoClose(workflowId);};
}
async function sendWorkflowTerminalInput(workflowId, text) {
  if(!text.trim()) return;
  if(text.trim()==='/clear'){const terminal=workflowTerminals.get(workflowId); if(terminal) terminal.output.textContent=''; return;}
  const sessionId=workflowTerminalSessions.get(workflowId);
  if(!sessionId){workflowTerminalWrite(workflowId,`No interactive session is active: ${text}\n`);return;}
  await request(`/api/terminal/${sessionId}/input`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:`${text}\n`})});
}
function openLlmActivity() {}
function llmActivityLabel(event) { const title=event.title || event.kind || 'Activity'; const model=event.metadata?.model || event.metadata?.endpoint || ''; return model ? `${title} · ${model}` : title; }
function llmUsageText(usage={}) { const input=usage.inputTokens || 0, output=usage.outputTokens || 0, total=usage.totalTokens || input + output; const elapsed=usage.elapsedSeconds ? `${usage.elapsedSeconds.toFixed(1)}s` : '--'; const rate=usage.tokensPerSecond ? `${usage.tokensPerSecond.toFixed(1)} tok/s` : '-- tok/s'; const mark=usage.estimated ? 'estimated' : 'actual'; return `input ${input.toLocaleString()} · output ${output.toLocaleString()} · total ${total.toLocaleString()} · ${elapsed} · ${rate} · ${mark}`; }
function addLlmCard(event, className, title, text, meta='') { openLlmActivity(); const stream=$('#llm-activity-stream'); const card=document.createElement('article'); card.className=`llm-card ${className}`; const head=document.createElement('div'); head.className='llm-card-title'; const label=document.createElement('span'); label.textContent=title; const small=document.createElement('small'); small.textContent=meta; head.append(label,small); const body=document.createElement('pre'); body.textContent=text || ''; card.append(head,body); stream.append(card); stream.scrollTop=stream.scrollHeight; return {card,body,small}; }
function updateLlmSummary(event, text='') { const usage=event.usage && Object.keys(event.usage).length ? ` · ${llmUsageText(event.usage)}` : ''; $('#llm-activity-summary').textContent=`${event.kind}: ${llmActivityLabel(event)}${text ? ` · ${text}` : ''}${usage}`; }
function codeFromModelText(text) { try{const parsed=JSON.parse(text); if(typeof parsed.code==='string') return parsed.code; if(Array.isArray(parsed.files)){const file=parsed.files.find(item=>item && typeof item.content==='string' && String(item.path || '').endsWith('.py')) || parsed.files.find(item=>item && typeof item.content==='string'); return file?.content || '';} }catch(error){} return ''; }
function routeLlmEventToNode(event) {
  const activity=event.metadata?.activity || '';
  if(event.kind==='start' && activity.startsWith('script-') && pendingLocalLlmNodeId){llmNodeTargets.set(event.runId,pendingLocalLlmNodeId);llmNodeStreamState.set(event.runId,{startedAt:event.createdAt || Date.now()/1000,tokens:0,hasCode:false});return true;}
  const nodeId=llmNodeTargets.get(event.runId);
  if(!nodeId)return false;
  const node=nodeById(nodeId);
  if(!node)return false;
  if(event.kind==='code_delta'){
    const stream=llmNodeStreamState.get(event.runId) || {startedAt:event.createdAt || Date.now()/1000,tokens:0,hasCode:false};
    if(!stream.hasCode){node.code='';stream.hasCode=true;}
    node.code+=(event.text || '');
    stream.tokens+=Math.max(1,Math.floor((event.text || '').length/4));
    llmNodeStreamState.set(event.runId,stream);
    state.runningNode=node.id;
    render();
    return true;
  }
  if(event.kind==='text'){
    const code=codeFromModelText(event.text || '');
    if(code){node.code=code;state.runningNode=node.id;render();return true;}
  }
  if(event.kind==='usage'){
    const stream=llmNodeStreamState.get(event.runId) || {};
    const usage=event.usage || {};
    const rate=usage.tokensPerSecond ? `${usage.tokensPerSecond.toFixed(1)} tok/s` : stream.tokens && event.createdAt && stream.startedAt ? `${(stream.tokens/Math.max(.1,event.createdAt-stream.startedAt)).toFixed(1)} tok/s` : '';
    if(rate){node.code=`# ${rate} · ${llmUsageText(usage)}\n` + (node.code || '');render();}
    return true;
  }
  if(event.kind==='complete' || event.kind==='error'){llmNodeTargets.delete(event.runId);llmNodeStreamState.delete(event.runId);return true;}
  return ['prompt','status','text_delta'].includes(event.kind);
}
function handleLlmActivityEvent(event) {
  lastLlmActivityId=Math.max(lastLlmActivityId,event.id||0);
  if(routeLlmEventToNode(event)) return;
  const elapsed=event.createdAt ? new Date(event.createdAt*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '';
  if(event.kind==='start'){llmDeltaCards.delete(event.runId);addLlmCard(event,'status',llmActivityLabel(event),event.text || 'started',elapsed);updateLlmSummary(event,'started');return;}
  if(event.kind==='prompt'){addLlmCard(event,'prompt',event.title || 'Prompt',event.text || '',elapsed);updateLlmSummary(event,'prompt sent');return;}
  if(event.kind==='status'){addLlmCard(event,'status',event.title || 'Status',event.text || '',elapsed);updateLlmSummary(event,event.text || 'working');return;}
  if(event.kind==='text_delta'){const key=event.runId || 'default'; let live=llmDeltaCards.get(key); if(!live){live=addLlmCard(event,'text-delta','Streaming output','',elapsed); live.startedAt=event.createdAt || Date.now()/1000; live.tokens=0; llmDeltaCards.set(key,live);} live.body.textContent+=(event.text || ''); live.tokens+=Math.max(1,Math.floor((event.text || '').length/4)); const seconds=Math.max(.1,(event.createdAt || Date.now()/1000)-live.startedAt); live.small.textContent=`~${live.tokens.toLocaleString()} tokens · ${(live.tokens/seconds).toFixed(1)} tok/s`; live.body.parentElement.parentElement.scrollTop=live.body.parentElement.parentElement.scrollHeight; updateLlmSummary(event,live.small.textContent);return;}
  if(event.kind==='text'){addLlmCard(event,'text-delta',event.title || 'Model output',event.text || '',elapsed);updateLlmSummary(event,'output received');return;}
  if(event.kind==='usage'){addLlmCard(event,'usage',event.title || 'Usage',llmUsageText(event.usage),elapsed);updateLlmSummary(event,'complete');return;}
  if(event.kind==='complete'){addLlmCard(event,'status',event.title || 'Complete',event.text || 'complete',elapsed);updateLlmSummary(event,'complete');return;}
  if(event.kind==='error'){addLlmCard(event,'error',event.title || 'Error',event.text || '',elapsed);updateLlmSummary(event,'error');return;}
  addLlmCard(event,'status',event.title || event.kind,event.text || JSON.stringify(event),elapsed);updateLlmSummary(event);
}
async function pollLlmActivity() { while(true){try{const data=await request(`/api/llm-activity/events?after=${lastLlmActivityId}&wait=1`); for(const event of data.events||[]) handleLlmActivityEvent(event);}catch(error){await sleep(1200);}} }
async function stopTerminalSession() { if(terminalEventSource){terminalEventSource.close();terminalEventSource=null;} if(activeTerminalSession){try{await request(`/api/terminal/${activeTerminalSession}/stop`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}catch(error){console.warn(error);} activeTerminalSession=null;} }
async function stopActiveWorkflowRun() { const runId=activeRunId; if(activeRunStream){activeRunStream.close();activeRunStream=null;} activeRunId=null; if(runId){try{await request(`/api/runs/${runId}/stop`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});}catch(error){console.warn(error);}} await stopTerminalSession(); clearRunVisuals(); render(); $('#run').textContent='▶ Run workflow'; $('#run').disabled=false; $('#terminal-input').placeholder='Send input to active session'; $('#save-state').textContent='Workflow run stopped'; }
async function closeMainTerminal() { if($('#run')?.disabled || activeRunId){await stopActiveWorkflowRun();} else if(activeTerminalSession){await stopTerminalSession(); $('#save-state').textContent='Terminal session stopped';} $('#terminal').hidden=true; }
async function showVerboseRecurringWorkflow(workflowId) {
  if(!workflowId || state.workflow.id===workflowId) return;
  if(state.workflow.id || state.workflow.nodes.length) await save();
  await load(workflowId);
}
function applyVisibleServiceEvent(event) {
  if(state.workflow.id!==event.workflowId) return;
  const text=String(event.text || '');
  if(text.includes('Recurring run started')){state.runs={};clearRunVisuals();render();return;}
  if(text.startsWith('Starting node:') && event.nodeId){state.runningNode=event.nodeId;nodeVisualStartedAt=performance.now();flashNode(event.nodeId,900);beginNodeHandoff(event.nodeId);render();scrollNodeIntoFocus(event.nodeId);return;}
  if(event.nodeStatus && event.nodeId){state.runs[event.nodeId]={id:event.nodeId,status:event.nodeStatus,outputs:event.outputs||{},error:event.level==='error'?text:''};state.runningNode=null;nodeVisualStartedAt=0;render();return;}
  if(text.includes('Recurring run finished') || text.includes('Recurring run failed')){clearRunVisuals();render();}
}
async function pollServiceEvents() { try{const data=await request('/api/workflow-service/events'); for(const event of data.events||[]){const key=event.id || `${event.at}:${event.workflowId}:${event.text}`; if(state.serviceEventKeys.has(key))continue; state.serviceEventKeys.add(key); if(!serviceEventsPrimed)continue; const workflow=(state.available||[]).find(item=>item.id===event.workflowId); const name=workflow?.name || event.workflowId; const text=String(event.text || ''); const isRunStart=text.includes('Recurring run started'); const isRunFinish=text.includes('Recurring run finished') || text.includes('Recurring run failed'); if(isRunStart && workflow?.recurring?.displayMode!=='silent'){recurringWorkflowTerminalRuns.add(event.workflowId);cancelWorkflowTerminalAutoClose(event.workflowId);} const shouldUseWorkflowTerminal=recurringWorkflowTerminalRuns.has(event.workflowId); if(shouldUseWorkflowTerminal && event.sessionId) attachWorkflowServiceTerminalSession(event.workflowId,event.sessionId,name); if(shouldUseWorkflowTerminal){const time=new Date(event.at*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}); workflowTerminalWrite(event.workflowId,`\n[${time}] ${event.text}\n`,name);} if(isRunFinish) finishWorkflowTerminalRun(event.workflowId);} serviceEventsPrimed=true; if(state.serviceEventKeys.size>300) state.serviceEventKeys=new Set([...state.serviceEventKeys].slice(-150));}catch(error){console.warn(error);} }
async function finalizeCodexProject(sessionId, code) { const project=codexProjectsBySession.get(sessionId); if(!project)return; codexProjectsBySession.delete(sessionId); const existing=nodeById(project.nodeId); if(code !== 0){if(existing){existing.name='Codex script failed'; existing.code+=`\n\n# Codex exited${code==null?'':` with code ${code}`}.`; if(state.runningNode===existing.id) state.runningNode=null; markDirty(); render();} terminalWrite('Codex project did not finish cleanly; node was not converted to a runnable script.\n'); return;} try{const result=await request('/api/generated-projects/task-details',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({projectDir:project.projectDir})}); const node=existing || taskNode(result.taskDetails); if(existing){updateTaskNodeFromDetails(node,result.taskDetails); node.type='task'; node.name=result.taskDetails.displayName || result.projectName || node.task.scriptPath.split('/').pop();} else {const center=visibleCanvasCenter(); node.x=center.x-120; node.y=Math.max(90,center.y-80); state.workflow.nodes.push(node);} node.includeInWorkflow=true; state.selected=node.id; if(state.runningNode===node.id) state.runningNode=null; markDirty(); render(); terminalWrite(`Created workflow node for ${result.projectName}.\n`);}catch(error){if(existing){existing.name='Codex script incomplete'; existing.code+=`\n\n# Codex finished, but no runnable Python entry point was found.\n# ${String(error.message).replaceAll('\n','\n# ')}`; if(state.runningNode===existing.id) state.runningNode=null; markDirty(); render();} terminalWrite(`Codex project finished, but no runnable node was created: ${error.message}\n`);} }
async function finalizeCodexEdit(sessionId, code) { const edit=codexEditsBySession.get(sessionId); if(!edit)return; codexEditsBySession.delete(sessionId); if(code!==0){terminalWrite('Codex edit did not finish cleanly; node code was not refreshed.\n');return;} try{const result=await request('/api/tasks/details',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scriptPath:edit.scriptPath})}); const node=nodeById(edit.nodeId); if(node){updateTaskNodeFromDetails(node,result); state.selected=node.id; markDirty(); render(); terminalWrite('Workflow node refreshed from edited script.\n');} else terminalWrite('Codex edit finished, but the workflow node no longer exists.\n');}catch(error){terminalWrite(`Codex edit finished, but the script could not be reloaded: ${error.message}\n`);} }
async function finalizeCodexAgent(sessionId, code) { const build=codexAgentBuildsBySession.get(sessionId); if(!build)return; codexAgentBuildsBySession.delete(sessionId); if(code!==0){terminalWrite('Codex agent build did not finish cleanly; node was not updated.\n');return;} try{const result=await request('/api/codex/agent-details',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({projectDir:build.projectDir})}); const node=nodeById(build.nodeId); if(node){node.name=result.name || 'Codex Agent'; node.code=result.systemPrompt; node.agent={...(node.agent||{}),systemPrompt:result.systemPrompt,purpose:build.brief,draft:false,claudiaDraft:false,codexDraft:false,builder:'codex',builtByCodex:true,projectDir:build.projectDir}; state.selected=node.id; markDirty(); render(); terminalWrite(`Codex agent built: ${node.name}\n`);} else terminalWrite('Codex agent finished, but the workflow node no longer exists.\n');}catch(error){terminalWrite(`Codex agent finished, but agent.json could not be loaded: ${error.message}\n`);} }
function connectTerminalSession(sessionId, title='Terminal session') { if(terminalEventSource) terminalEventSource.close(); activeTerminalSession=sessionId; terminalSection(title); terminalEventSource=new EventSource(`/api/terminal/${sessionId}/events`); terminalEventSource.onmessage=async event=>{const message=JSON.parse(event.data); if(message.type==='start') terminalWrite(`Session ${message.kind} started in ${message.cwd}\n`); else if(message.type==='output') terminalWrite(message.text); else if(message.type==='exit'){terminalWrite(`\nProcess exited${message.code==null?'':` with code ${message.code}`}.\n`); await finalizeCodexProject(sessionId,message.code); await finalizeCodexEdit(sessionId,message.code); await finalizeCodexAgent(sessionId,message.code); terminalEventSource.close(); terminalEventSource=null; activeTerminalSession=null;} else if(message.type==='error') terminalWrite(`\nERROR: ${message.text}\n`);}; terminalEventSource.onerror=()=>{}; }
async function sendTerminalInput(text) { if(!text.trim()) return; if(text.trim()==='/clear'){clearTerminal();return;} if(!activeTerminalSession){terminalWrite(`\nNo interactive session is active: ${text}\n`);return;} await request(`/api/terminal/${activeTerminalSession}/input`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:`${text}\n`})}); }
function sleep(ms) { return new Promise(resolve=>window.setTimeout(resolve, ms)); }
function stopCodeScroller() { if(codeScroll.raf) cancelAnimationFrame(codeScroll.raf); codeScroll.nodeId=null; codeScroll.startedAt=0; codeScroll.raf=null; }
function syncCodeScroller() { const node=nodeById(state.runningNode); const shouldScroll=node && node.type!=='input' && node.type!=='output'; const preview=shouldScroll ? world.querySelector(`.node[data-id="${state.runningNode}"] .node-code-preview:not([hidden])`) : null; if(!preview){stopCodeScroller();return;} if(codeScroll.nodeId!==state.runningNode){stopCodeScroller();codeScroll.nodeId=state.runningNode;codeScroll.startedAt=performance.now();} if(codeScroll.raf)return; const tick=now=>{if(!state.runningNode||state.runningNode!==codeScroll.nodeId){stopCodeScroller();return;} const current=world.querySelector(`.node[data-id="${codeScroll.nodeId}"] .node-code-preview:not([hidden])`); if(!current){stopCodeScroller();return;} const maxScroll=Math.max(0,current.scrollHeight-current.clientHeight); if(maxScroll>0){const cycleMs=30000;const progress=((now-codeScroll.startedAt)%cycleMs)/cycleMs;current.scrollTop=progress<.92 ? maxScroll*(progress/.92) : 0;} codeScroll.raf=requestAnimationFrame(tick);}; codeScroll.raf=requestAnimationFrame(tick); }
function clearRunVisuals() { stopCodeScroller(); state.runningNode=null; state.transmittingEdges=new Set(); state.flashNodes=new Set(); }
function scrollNodeIntoFocus(nodeId) { const node=nodeById(nodeId); if(!node)return; const width=node.width || (node.type==='input'?330:235); const height=node.height || 260; const targetLeft=Math.max(0, node.x - Math.max(90, canvas.clientWidth * 0.18)); const targetTop=Math.max(0, node.y - Math.max(40, (canvas.clientHeight - height) * 0.28)); canvas.scrollTo({left:targetLeft,top:targetTop,behavior:'smooth'}); }
function flashNode(nodeId, duration=750) { state.flashNodes.add(nodeId); window.setTimeout(()=>{state.flashNodes.delete(nodeId); const element=world.querySelector(`.node[data-id="${nodeId}"]`); if(element) element.classList.remove('flash');},duration); }
function beginNodeHandoff(targetNodeId) { const incoming=state.workflow.edges.filter(edge=>edge.target===targetNodeId); state.transmittingEdges=new Set(incoming.map(edgeKey)); for(const edge of incoming) flashNode(edge.source, 650); }
function resetWorkflowView() { clearRunVisuals(); state.runs={}; state.workflow.nodes.forEach(clearNodeRunState); markDirty(); canvas.scrollTo({left:0,top:0,behavior:'smooth'}); render(); }
async function finishRunningNodeDelay(minMs=900) { if(!nodeVisualStartedAt)return; const elapsed=performance.now()-nodeVisualStartedAt; if(elapsed<minMs) await sleep(minMs-elapsed); }
async function handleRunMessage(message, stream) { if(message.type==='terminal_session'){attachWorkflowTerminalSession(message.sessionId);terminalWrite('\nInteractive workflow terminal attached.\n');return;} if(message.type==='node_start'){await finishRunningNodeDelay(350);state.runningNode=message.nodeId;nodeVisualStartedAt=performance.now();flashNode(message.nodeId, 900);beginNodeHandoff(message.nodeId);render();scrollNodeIntoFocus(message.nodeId);terminalWrite(`\n▶ ${message.name}\n`);window.setTimeout(()=>{state.transmittingEdges=new Set();render();},700);return;} if(message.type==='log'){terminalWrite(message.text);return;} if(message.type==='node_complete'){await finishRunningNodeDelay(900);state.runs[message.nodeId]={id:message.nodeId,status:message.status,outputs:message.outputs||{},error:message.error||''};state.runningNode=null;nodeVisualStartedAt=0;render();const symbol=message.status==='success'?'✓':message.status==='skipped'?'•':'✗';terminalWrite(`\n${symbol} ${message.status}\n`);return;} if(message.type==='complete'){const completed=Object.fromEntries(message.run.nodes.map(node=>[node.id,node]));state.runs=activeRunMode==='whole'?completed:{...state.runs,...completed};captureRunOutputsForSave();if(activeRunMode==='whole'||activeRunMode==='before-break') await save();const failure=workflowRunError(message.run);terminalWrite(failure?`\nWorkflow FAILED: ${failure}\n`:'\nWorkflow finished successfully.\n');$('#save-state').textContent=failure?'Workflow failed':'Workflow finished';stream.close();activeRunStream=null;activeRunId=null;activeTerminalSession=null;$('#terminal-input').placeholder='Send input to active session';await sleep(650);clearRunVisuals();render();$('#run').textContent='▶ Run workflow';$('#run').disabled=false;return;} if(message.type==='error'){clearRunVisuals();render();terminalWrite(`\nERROR: ${message.text}\n`);stream.close();activeRunStream=null;activeRunId=null;activeTerminalSession=null;$('#terminal-input').placeholder='Send input to active session';$('#run').textContent='▶ Run workflow';$('#run').disabled=false;} }
async function run(mode='whole') { if($('#run').disabled) return; activeRunMode=mode; terminalSection(mode==='after-break'?'Right workstation run':mode==='before-break'?'Left workstation run':'Workflow run'); terminalWrite('Saving current workflow...\n'); $('#run').textContent='Running…'; $('#run').disabled=true; clearRunStateForMode(mode); clearRunVisuals(); runEventQueue=Promise.resolve(); nodeVisualStartedAt=0; try{if(mode==='after-break') await syncBreakpointCacheFromSaved(); await save(); if(!state.workflow.id)return; terminalWrite(mode==='after-break'?'Starting workflow after break…\n':mode==='before-break'?'Starting workflow up to break…\n':'Starting workflow…\n');const data=await request(`/api/workflows/${state.workflow.id}/run`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});activeRunId=data.runId;const stream=new EventSource(`/api/runs/${data.runId}/events`);activeRunStream=stream;stream.onmessage=event=>{const message=JSON.parse(event.data);runEventQueue=runEventQueue.then(()=>handleRunMessage(message,stream)).catch(error=>{clearRunVisuals();render();terminalWrite(`\nERROR: ${error.message}\n`);stream.close();activeRunStream=null;activeRunId=null;$('#run').textContent='▶ Run workflow';$('#run').disabled=false;});};stream.onerror=()=>{if(stream.readyState===EventSource.CLOSED){activeRunStream=null;activeRunId=null;$('#run').textContent='▶ Run workflow';$('#run').disabled=false;}};}catch(error){clearRunVisuals();render();terminalWrite(`ERROR: ${error.message}\n`);activeRunStream=null;activeRunId=null;$('#run').textContent='▶ Run workflow';$('#run').disabled=false;} }
function newWorkflow(){clearTimeout(layoutSaveTimer);saveQueued=false;workflowRevision=0;state.workflow={id:null,name:'Untitled workflow',nodes:[],edges:[],recurring:{enabled:false,frequencySeconds:300}};state.selected=null;state.runs={};clearLinkMode();clearRunVisuals();$('#save-state').textContent='Not saved';render();}
function closeMenus() { document.querySelectorAll('.menu-group').forEach(group=>group.classList.remove('open')); }
document.querySelectorAll('.menu-trigger').forEach(trigger=>trigger.onclick=event=>{event.stopPropagation();const group=trigger.parentElement;const wasOpen=group.classList.contains('open');closeMenus();group.classList.toggle('open',!wasOpen);});
document.addEventListener('click', event=>{closeMenus(); if(!event.target.closest('.workflow-row')) closeWorkflowActions(); if (!event.target.closest('#quick-create') && !event.target.closest('.node-add')) $('#quick-create').hidden=true; if (!event.target.closest('#node-menu') && !event.target.closest('.node-menu-button')) $('#node-menu').hidden=true; if (!event.target.closest('#link-node-menu') && !event.target.closest('#quick-create')) $('#link-node-menu').hidden=true; if (!event.target.closest('#unlink-node-menu') && !event.target.closest('#quick-create') && !event.target.closest('#node-menu')) $('#unlink-node-menu').hidden=true; if (!event.target.closest('#canvas-create') && !event.target.closest('#canvas-add') && !event.target.closest('.workstation-controls')) $('#canvas-create').hidden=true; if (!event.target.closest('#claudia-menu') && !event.target.closest('#claudia-dock')) $('#claudia-menu').hidden=true; if (!event.target.closest('#codex-menu') && !event.target.closest('#codex-dock')) $('#codex-menu').hidden=true; if (!event.target.closest('#start-create') && !event.target.closest('#empty-add')) $('#start-create').hidden=true;});
document.querySelectorAll('#quick-create button[data-kind]').forEach(button=>button.onclick=()=>{const source=nodeById($('#quick-create').dataset.source); if(button.dataset.kind==='link') startLinkMode($('#quick-create').dataset.source); else if(button.dataset.kind==='task') addTask(source); else createLinkedNode(button.dataset.kind);});
document.querySelectorAll('#node-menu button[data-node-action]').forEach(button=>button.onclick=event=>{event.stopPropagation();const nodeId=$('#node-menu').dataset.node;if(button.dataset.nodeAction==='rename'){renameNodePrompt(nodeId);$('#node-menu').hidden=true;} else if(button.dataset.nodeAction==='build-agent') buildDraftAgent(nodeId); else if(button.dataset.nodeAction==='clear-breakpoint') clearBreakpointNode(nodeId); else if(button.dataset.nodeAction==='edit-local-llm') editTaskWithLocalLlm(nodeId); else if(button.dataset.nodeAction==='edit-codex') editTaskWithCodex(nodeId); else if(button.dataset.nodeAction==='toggle-include') toggleIncludeNode(nodeId); else if(button.dataset.nodeAction==='link') startLinkMode(nodeId); else if(button.dataset.nodeAction==='unlink') unlinkNode(nodeId); else if(button.dataset.nodeAction==='delete'){removeNode(nodeId);$('#node-menu').hidden=true;}});
document.querySelectorAll('#canvas-create button[data-create-kind]').forEach(button=>button.onclick=()=>createStandaloneNode(button.dataset.createKind));
document.querySelectorAll('#claudia-menu button[data-claudia-action]').forEach(button=>button.onclick=()=>{if(button.dataset.claudiaAction==='spawn-agent')spawnSpecialistAgent(); else if(button.dataset.claudiaAction==='create-script')createClaudiaScript(); else if(button.dataset.claudiaAction==='start-llm')controlLlm('start'); else if(button.dataset.claudiaAction==='stop-llm')controlLlm('stop'); else if(button.dataset.claudiaAction==='delete-agent')deleteCurrentAgent();});
document.querySelectorAll('#codex-menu button[data-codex-action]').forEach(button=>button.onclick=()=>{if(button.dataset.codexAction==='workflow-terminal')openCodexWorkflowTerminal(); else if(button.dataset.codexAction==='spawn-agent')spawnSpecialistAgent('codex'); else if(button.dataset.codexAction==='create-script')createCodexScript(); else if(button.dataset.codexAction==='start-llm')controlLlm('start','codex'); else if(button.dataset.codexAction==='stop-llm')controlLlm('stop','codex'); else if(button.dataset.codexAction==='delete-agent')deleteCurrentAgent('codex');});
document.querySelectorAll('#start-create button[data-start-kind]').forEach(button=>button.onclick=()=>{if(button.dataset.startKind==='task') addTask(); else if(button.dataset.startKind==='file'){const center=visibleCanvasCenter();addDataFileNode({x:center.x-145,y:Math.max(90,center.y-90)}).catch(error=>alert(error.message));} else if(button.dataset.startKind==='product'){const center=visibleCanvasCenter();addProductNode({x:center.x-145,y:Math.max(90,center.y-90)}).catch(error=>alert(error.message));} else if(button.dataset.startKind==='agent'){const center=visibleCanvasCenter();const node=draftAgentNode({x:center.x-180,y:Math.max(24,center.y-120)});state.workflow.nodes.push(node);state.selected=node.id;$('#start-create').hidden=true;markDirty();render();} else addStarterInput();});
$('#close-terminal').onclick=()=>closeMainTerminal().catch(error=>terminalWrite(`\nERROR: ${error.message}\n`));
$('#close-llm-activity').onclick=()=>{$('#llm-activity').hidden=true;};
$('#terminal-input-form').addEventListener('submit', event=>{event.preventDefault();const input=$('#terminal-input');const text=input.value;input.value='';sendTerminalInput(text).catch(error=>terminalWrite(`\nERROR: ${error.message}\n`));});
$('#add-node').onclick=addNode; $('#canvas-add').onclick=event=>{event.stopPropagation();showCanvasCreate();}; $('#claudia-menu-button').onclick=event=>{event.stopPropagation();showClaudiaMenu();}; $('#codex-menu-button').onclick=event=>{event.stopPropagation();showCodexMenu();}; $('#empty-add').onclick=()=>{positionStartControls();$('#start-create').hidden=false;}; $('#reset-view').onclick=resetWorkflowView; $('#menu-new').onclick=newWorkflow; $('#save').onclick=save; $('#menu-save').onclick=save; $('#run').onclick=()=>run(); $('#menu-file').onclick=()=>addDataFileNode(standalonePosition()).catch(error=>alert(error.message)); $('#menu-product').onclick=()=>addProductNode(standalonePosition()).catch(error=>alert(error.message)); $('#menu-task').onclick=()=>addTask(); $('#menu-python').onclick=addNode; $('#show-inspector').onclick=openInspector; $('#menu-inspector').onclick=openInspector; $('#menu-terminal').onclick=openTerminal; $('#menu-workflows').onclick=()=>{$('#app-layout').classList.remove('workflow-sidebar-closed');$('#workflow-drawer-toggle').setAttribute('aria-expanded','true');}; $('#workflow-drawer-toggle').onclick=()=>{const layout=$('#app-layout');layout.classList.toggle('workflow-sidebar-closed');$('#workflow-drawer-toggle').setAttribute('aria-expanded',!layout.classList.contains('workflow-sidebar-closed'));setTimeout(drawLinks,220);}; $('#active-workflow-toggle').onclick=()=>{const panel=$('#bottom-service-panel');panel.classList.toggle('collapsed');$('#active-workflow-toggle').setAttribute('aria-expanded',!panel.classList.contains('collapsed'));setTimeout(()=>{syncBottomServiceHeight();drawLinks();render();},220);}; $('#workflow-name').addEventListener('input',e=>{state.workflow.name=e.target.value;markDirty();}); $('#close-inspector').onclick=()=>{state.selected=null;closeInspector();render();}; document.addEventListener('keydown',event=>{if(event.key==='Escape'&&state.pendingLinkSource){clearLinkMode();$('#save-state').textContent=state.workflow.id?'Saved':'Not saved';render();}if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='s'){event.preventDefault();save();}if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='n'){event.preventDefault();newWorkflow();}}); bindNodeForm(); canvas.addEventListener('pointerdown',panCanvas); canvas.addEventListener('scroll',()=>{positionStartControls();renderWorkstationControls();drawLinks();}); window.addEventListener('resize',()=>{syncBottomServiceHeight();positionStartControls();renderWorkstationControls();drawLinks();}); refreshProducts().catch(console.warn); refreshDataFiles().then(()=>render()).catch(console.warn); loadWorkflows().then(pollServiceEvents).catch(error=>alert(error.message)); pollLlmActivity(); window.setInterval(()=>{if(state.available && !workflowMenuOpen())renderWorkflows();},1000); window.setInterval(()=>loadWorkflows().then(pollServiceEvents).catch(console.warn),1000); render();
