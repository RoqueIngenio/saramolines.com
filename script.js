// Textos editables: se leen de textos.js y se colocan en su sitio.
// Si textos.js falta o tiene un error, se quedan los textos que ya trae la página.
function formatear(texto) {
  return texto
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/==(.+?)==/g, '<span class="resaltado">$1</span>');
}

function leerTextos(bruto) {
  const textos = {};
  let clave = null;
  bruto.split("\n").forEach((linea) => {
    const etiqueta = linea.trim().match(/^\[([\w.\-]+)\]$/);
    if (etiqueta) {
      clave = etiqueta[1];
      textos[clave] = [];
    } else if (clave && !linea.trim().startsWith("##")) {
      textos[clave].push(linea);
    }
  });
  Object.keys(textos).forEach((k) => (textos[k] = textos[k].join("\n").trim()));
  return textos;
}

function colocarTextos() {
  if (typeof window.TEXTOS_WEB !== "string") return;
  const textos = leerTextos(window.TEXTOS_WEB);
  document.querySelectorAll("[data-texto]").forEach((el) => {
    const texto = textos[el.dataset.texto];
    if (!texto) return;
    const lineas = texto.split("\n").map((l) => l.trim()).filter(Boolean);
    switch (el.dataset.formato) {
      case "parrafos":
        el.innerHTML = texto.split(/\n\s*\n/).map((p) => `<p>${formatear(p.trim())}</p>`).join("");
        break;
      case "lista":
        el.innerHTML = lineas.map((l) => `<li>${formatear(l)}</li>`).join("");
        break;
      default:
        el.innerHTML = formatear(lineas.join(" "));
    }
  });
}

try {
  colocarTextos();
} catch (error) {
  console.warn("No se pudieron cargar los textos de textos.js:", error);
}

// Menú del móvil: abrir y cerrar
const botonMenu = document.querySelector(".boton-menu");
const menu = document.querySelector(".menu");

if (botonMenu && menu) {
  const cerrar = () => {
    menu.classList.remove("abierto");
    botonMenu.classList.remove("abierto");
    botonMenu.setAttribute("aria-expanded", false);
  };
  botonMenu.addEventListener("click", () => {
    const abierto = menu.classList.toggle("abierto");
    botonMenu.classList.toggle("abierto", abierto);
    botonMenu.setAttribute("aria-expanded", abierto);
  });
  menu.querySelectorAll("a").forEach((enlace) => enlace.addEventListener("click", cerrar));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") cerrar(); });
}

// Animación suave: los bloques aparecen al bajar con el scroll
const bloques = document.querySelectorAll(".aparece");
document.documentElement.classList.add("con-js");

if ("IntersectionObserver" in window) {
  const observador = new IntersectionObserver(
    (entradas) => {
      entradas.forEach((entrada) => {
        if (entrada.isIntersecting) {
          entrada.target.classList.add("visible");
          observador.unobserve(entrada.target);
        }
      });
    },
    { threshold: 0.12 }
  );
  bloques.forEach((bloque) => observador.observe(bloque));
} else {
  bloques.forEach((bloque) => bloque.classList.add("visible"));
}

// Buscador del blog: filtra las tarjetas por palabras y por tema, sin recargar la página.
// La selección se guarda en la dirección (?q=...&tema=...) para poder compartirla.
const buscador = document.querySelector("[data-buscador]");

if (buscador) {
  const campo = buscador.querySelector("input");
  const filtros = buscador.querySelectorAll(".filtro");
  const cuenta = buscador.querySelector(".buscador-cuenta");
  const tarjetas = document.querySelectorAll(".articulo-tarjeta[data-buscar]");
  const vacio = document.querySelector(".buscador-vacio");
  const sinTildes = (t) => t.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  let tema = "";

  const filtrar = () => {
    const palabras = sinTildes(campo.value).split(/\s+/).filter(Boolean);
    let visibles = 0;
    tarjetas.forEach((tarjeta) => {
      const coincide =
        (!tema || tarjeta.dataset.tema.split(" ").includes(tema)) &&
        palabras.every((p) => tarjeta.dataset.buscar.includes(p));
      tarjeta.hidden = !coincide;
      if (coincide) visibles++;
    });
    filtros.forEach((f) => {
      const activo = f.dataset.tema === tema;
      f.classList.toggle("activo", activo);
      f.setAttribute("aria-pressed", activo);
    });
    cuenta.textContent = visibles === 1 ? "1 texto" : `${visibles} textos`;
    vacio.hidden = visibles > 0;
    const parametros = new URLSearchParams();
    if (campo.value.trim()) parametros.set("q", campo.value.trim());
    if (tema) parametros.set("tema", tema);
    const consulta = parametros.toString();
    try {
      history.replaceState(null, "", consulta ? `?${consulta}` : location.pathname);
    } catch (error) { /* abierta con doble clic: la dirección no se puede cambiar */ }
  };

  const inicial = new URLSearchParams(location.search);
  campo.value = inicial.get("q") || "";
  tema = [...filtros].some((f) => f.dataset.tema === inicial.get("tema")) ? inicial.get("tema") : "";

  campo.addEventListener("input", filtrar);
  filtros.forEach((f) => f.addEventListener("click", () => { tema = f.dataset.tema; filtrar(); }));
  const limpiar = document.querySelector("[data-limpiar]");
  if (limpiar) limpiar.addEventListener("click", () => { campo.value = ""; tema = ""; filtrar(); campo.focus(); });
  filtrar();
}

// Testimonios: al tocar una tarjeta gira y enseña el mensaje (otra vez, vuelve)
document.querySelectorAll(".tarjeta-giro").forEach((tarjeta) => {
  tarjeta.addEventListener("click", () => {
    const girada = tarjeta.getAttribute("aria-pressed") === "true";
    tarjeta.setAttribute("aria-pressed", String(!girada));
  });
});
