const bridge = window.AstrBotPluginView;
const items = document.getElementById("items");
const status = document.getElementById("status");
const summary = document.getElementById("summary");
const pageLabel = document.getElementById("page");
const previous = document.getElementById("prev");
const next = document.getElementById("next");
const refresh = document.getElementById("refresh");
const search = document.getElementById("search");
const perPage = 12;
let assets = [];
let page = 1;
let renderGeneration = 0;

function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

function matchingAssets() {
  const query = search.value.trim().toLocaleLowerCase();
  return assets.filter((asset) => !query || `${asset.name} ${asset.ordinal}`.toLocaleLowerCase().includes(query));
}

function hasUnsavedChanges() {
  return assets.some((asset) => asset.name !== asset.savedName || asset.description !== asset.savedDescription);
}

function makeCard(asset) {
  const card = element("article", "card");
  const imageBox = element("div", "image-box");
  const image = element("img", "preview");
  image.alt = `奶蛙 #${asset.ordinal} 的预览`;
  const placeholder = element("span", "placeholder", "加载预览…");
  image.hidden = true;
  imageBox.append(image, placeholder);

  const body = element("div", "card-body");
  const heading = element("div", "card-heading");
  heading.append(element("strong", "", `#${asset.ordinal}`), element("span", asset.active ? "badge" : "badge inactive", asset.active ? "参与抽取" : "历史素材"));
  const nameLabel = element("label", "field", "图片名称");
  const name = element("input");
  name.type = "text";
  name.maxLength = 100;
  name.placeholder = "未填写时不显示名称";
  name.value = asset.name;
  nameLabel.append(name);
  const descriptionLabel = element("label", "field", "文字介绍");
  const description = element("textarea");
  description.maxLength = 2000;
  description.rows = 3;
  description.placeholder = "为这张奶蛙写一段介绍";
  description.value = asset.description;
  descriptionLabel.append(description);
  const actions = element("div", "card-actions");
  const message = element("span", "save-message");
  message.setAttribute("role", "status");
  const save = element("button", "primary", "保存");
  save.type = "button";
  name.addEventListener("input", () => { asset.name = name.value; message.textContent = "未保存"; });
  description.addEventListener("input", () => { asset.description = description.value; message.textContent = "未保存"; });
  save.addEventListener("click", async () => {
    save.disabled = true;
    const submittedName = asset.name;
    const submittedDescription = asset.description;
    message.textContent = "保存中…";
    try {
      const result = await bridge.apiPost("assets/save", { id: asset.id, name: submittedName, description: submittedDescription });
      asset.savedName = result.name;
      asset.savedDescription = result.description;
      if (asset.name === submittedName && asset.description === submittedDescription) {
        asset.name = name.value = result.name;
        asset.description = description.value = result.description;
        message.textContent = "已保存";
      } else {
        message.textContent = "有未保存的修改";
      }
    } catch (error) {
      message.textContent = `保存失败：${error.message || error}`;
    } finally {
      save.disabled = false;
    }
  });
  if (asset.name !== asset.savedName || asset.description !== asset.savedDescription) message.textContent = "未保存";
  actions.append(message, save);
  body.append(heading, nameLabel, descriptionLabel, actions);
  card.append(imageBox, body);
  return { card, image, placeholder, asset };
}

async function loadPreviews(cards, generation) {
  let index = 0;
  const worker = async () => {
    while (index < cards.length && generation === renderGeneration) {
      const item = cards[index++];
      try {
        const result = await bridge.apiGet("assets/preview", { id: item.asset.id });
        if (generation !== renderGeneration) return;
        item.image.src = `data:image/jpeg;base64,${result.image}`;
        item.image.hidden = false;
        item.placeholder.hidden = true;
      } catch (_) {
        item.placeholder.textContent = "预览不可用";
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(4, cards.length) }, worker));
}

function render() {
  const selected = matchingAssets();
  const pages = Math.max(1, Math.ceil(selected.length / perPage));
  page = Math.min(Math.max(1, page), pages);
  summary.textContent = `共 ${selected.length} 张素材`;
  pageLabel.textContent = `${page} / ${pages}`;
  previous.disabled = page === 1;
  next.disabled = page === pages;
  items.replaceChildren();
  const cards = selected.slice((page - 1) * perPage, page * perPage).map(makeCard);
  cards.forEach(({ card }) => items.append(card));
  if (!cards.length) items.append(element("p", "empty", assets.length ? "没有匹配的素材。" : "暂无素材。请先在插件配置中上传图片或 ZIP。"));
  renderGeneration += 1;
  void loadPreviews(cards, renderGeneration);
}

async function loadAssets() {
  if (hasUnsavedChanges()) {
    status.textContent = "还有未保存的修改，请先保存后再刷新。";
    return;
  }
  refresh.disabled = true;
  status.textContent = "正在读取素材…";
  try {
    const result = await bridge.apiGet("assets");
    assets = result.assets.map((asset) => ({ ...asset, name: asset.name || "", description: asset.description || "", savedName: asset.name || "", savedDescription: asset.description || "" }));
    page = 1;
    status.textContent = "";
    render();
  } catch (error) {
    status.textContent = `读取失败：${error.message || error}。请检查已上传的素材和插件日志。`;
  } finally {
    refresh.disabled = false;
  }
}

previous.addEventListener("click", () => { page -= 1; render(); });
next.addEventListener("click", () => { page += 1; render(); });
search.addEventListener("input", () => { page = 1; render(); });
refresh.addEventListener("click", loadAssets);
try {
  if (!bridge) throw new Error("未找到 AstrBot 视图桥接接口，请从 AstrBot 插件视图打开此页面");
  await bridge.ready();
  await loadAssets();
} catch (error) {
  status.textContent = `无法打开素材管理：${error.message || error}`;
}
