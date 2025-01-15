const socket = io.connect('http://' + document.domain + ':' + location.port);

// Configuración del gráfico de barras
let layout = {
    plot_bgcolor: "#f9f9f9", // Fondo blanco para gráficos
    paper_bgcolor: "#efefef", // Fondo gris claro para todo
    font: { color: "#555555" }, // Texto gris oscuro
    title: {
        font: { color: "#555555" } // Azul cielo suave para títulos
    },
    xaxis: {
        color: "#555555",
        titlefont: { color: "#555555" }
    },
    yaxis: {
        color: "#555555",
        titlefont: { color: "#555555" }
    }
};

let data = [{
    x: [],
    y: [],
    type: 'bar',
    marker: {
        color: [],
        opacity: 0.9,
        line: { width: 2, color: "white" }
    },
    text: [],
    textposition: 'auto',
    textfont: { size: 12, color: "white" },
    hovertemplate: '<b>%{x}</b><br>Yield: %{y:.2f}%<extra></extra>'
}];

Plotly.newPlot("chart", data, layout);

// Variables para el historial del monitor de pulsos
let pulseHistoryX = {};
let pulseHistoryY = {};
let inactiveTimeByLine = {};
let lastUpdateByLine = {};

// Función para formatear el tiempo acumulado
const formatTime = (seconds) => {
    let hours = Math.floor(seconds / 3600);
    let minutes = Math.floor((seconds % 3600) / 60);
    let secs = Math.floor(seconds % 60);
    if (hours > 0) {
        return `${hours}h ${minutes}m ${secs}s`;
    } else if (minutes > 0) {
        return `${minutes}m ${secs}s`;
    } else {
        return `${secs}s`;
    }
};

socket.emit('get_pulse_history');

// Manejar datos del historial enviados por el backend
socket.on('pulse_history_data', (data) => {
    console.log("Recibiendo estado inicial desde el backend:", data);

    // Limpieza explícita de datos locales antes de sincronizar
    pulseHistoryX = {};
    pulseHistoryY = {};
    inactiveTimeByLine = {};
    lastUpdateByLine = {};

    // Actualizar con datos recibidos
    pulseHistoryX = data.pulseHistoryX || {};
    pulseHistoryY = data.pulseHistoryY || {};
    inactiveTimeByLine = data.inactiveTimeByLine || {};
    lastUpdateByLine = data.lastUpdateByLine || {};

    actualizarGraficos(); // Refrescar gráficos con datos nuevos
});


// Enviar actualizaciones al backend
const updatePulseHistory = () => {
    console.log("Sincronizando datos con el backend...");

    // Validar que los datos enviados estén limpios
    console.log("Datos enviados al backend:", { pulseHistoryX, pulseHistoryY, inactiveTimeByLine, lastUpdateByLine });

    socket.emit('update_pulse_history', {
        pulseHistoryX,
        pulseHistoryY,
        inactiveTimeByLine,
        lastUpdateByLine
    });
};


// Manejar datos recibidos desde el backend
socket.on('update_data', (receivedData) => {
    console.log("Datos recibidos:", receivedData);
    let x = [], y = [], colors = [], text = [];
    let gaugeData = [];
    let currentTime = Date.now();

    receivedData.forEach((item, index) => {
        let line = item.label;

        if (!pulseHistoryX[line]) pulseHistoryX[line] = [];
        if (!pulseHistoryY[line]) pulseHistoryY[line] = [];
        if (!inactiveTimeByLine[line]) inactiveTimeByLine[line] = 0;
        if (!lastUpdateByLine[line]) lastUpdateByLine[line] = currentTime;

        let lastState = pulseHistoryY[line].length > 0 ? pulseHistoryY[line][pulseHistoryY[line].length - 1] : null;
        let elapsedTime = (currentTime - lastUpdateByLine[line]) / 1000;

        if (lastState === 0) {
            inactiveTimeByLine[line] += elapsedTime;
        }

        lastUpdateByLine[line] = currentTime;

        pulseHistoryX[line].push(new Date(currentTime).toLocaleTimeString());
        pulseHistoryY[line].push(item.state);

        if (pulseHistoryX[line].length > 20) pulseHistoryX[line].shift();
        if (pulseHistoryY[line].length > 20) pulseHistoryY[line].shift();

        x.push(item.label);
        y.push(item.yield);

        // Asignar colores según el yield
        if (item.yield >= 95) {
            colors.push("rgba(168, 213, 186, 0.9)"); // Verde pastel
        } else if (item.yield >= 90) {
            colors.push("rgba(255, 228, 161, 0.9)"); // Amarillo suave
        } else {
            colors.push("rgba(247, 168, 168, 0.9)"); // Rojo claro
        }
        

        // Etiquetas con datos adicionales
        let labelText = `Yield: ${item.yield.toFixed(2)}%\n<br>Pasa: ${item.passed}\n<br>Falla: ${item.failed}`;
        labelText += `\n<br>Disponibilidad: ${(item.availability * 100).toFixed(2)}%`;
        labelText += `\n<br>Rendimiento: ${(item.performance * 100).toFixed(2)}%`;
        if (item.reference && item.reference !== "N/A") {
            labelText += `\n<br><br>NP: ${item.reference}`;
        }
        if (item.test_name && item.test_name !== "N/A") {
            labelText += `\n<br><br>NP: ${item.test_name}`;
        }
        if (item.nombre_prueba && item.nombre_prueba !== "N/A") {
            labelText += `\n<br><br>NP: ${item.nombre_prueba}`;
        }
        labelText += `\n<br>TC: ${item.avg_cycle_time.toFixed(2)}s`;
        //labelText += `\n<br>OEE: ${(item.oee * 100).toFixed(2)}%`;

        text.push(labelText);

        // Configurar cada Gauge en la base
        // Configurar cada Gauge con tamaño reducido
        gaugeData.push({
            type: "indicator",
            mode: "gauge+number",
            value: item.oee * 100,
            title: { 
                text: "POEE", // Puedes incluir texto aquí si lo necesitas
                font: { size: 20, color: "#555555" } // Reducir tamaño del título
            },
            number: { 
                suffix: "%" // Agregar el símbolo de porcentaje
            },
            gauge: {
                axis: { range: [0, 100], tickwidth: 1, tickcolor: "black" },
                bar: { color: "rgba(114, 196, 149, 0.9)" },
                steps: [
                    { range: [0, 50], color: "rgba(255,126,99,0.9)" }, // Rojo intermedio pastel
                    { range: [50, 75], color: "rgba(255,231,64,0.9)" }, // Amarillo intermedio pastel
                    { range: [75, 100], color: "rgba(114,196,149,0.9)" } // Verde intermedio pastel
                ]
            
            },
            domain: { 
                x: [index / receivedData.length, (index + 1) / receivedData.length], // Distribuir horizontalmente
                y: [-0.2, 0.3] // Reducir altura de los Gauges
            }
        });

    });

    // Actualizar gráfico con barras y Gauges
    Plotly.react("chart", [...gaugeData, {
        x: x,
        y: y,
        type: 'bar',
        marker: { color: colors },
        text: text,
        textposition: "auto",
        hoverinfo: "text"
    }], layout);

    actualizarGraficos();
});

// Manejar el reinicio del monitor de actividad
socket.on('reset_activity_monitor', () => {
    console.log("Reiniciando el monitor de actividad...");
    pulseHistoryX = {};
    pulseHistoryY = {};
    inactiveTimeByLine = {};
    lastUpdateByLine = {};
    updatePulseHistory(); // Envía los datos reiniciados al backend
    actualizarGraficos();
});

// Actualizar gráficos secundarios                                              aqui aparecen centrado el grafico de pulsos
const actualizarGraficos = () => {
    let uniqueFALines = Object.keys(pulseHistoryX).sort().reverse();
    let yPositions = uniqueFALines.map((_, index) => index);

    if (uniqueFALines.length === 0) {
        Plotly.react("pulse_chart", [], {
            title: "",
            xaxis: { 
                title: "", 
                color: "black", // Texto negro
                tickfont: { color: "black", size: 12 } // Fuente negra para ticks
            },
            yaxis: { 
                title: "FA Lines", 
                color: "black", // Texto negro
                tickfont: { color: "black", size: 12 }, // Fuente negra para ticks
                titlefont: { color: "#007399", size: 16 } // Azul cielo suave para títulos
            },
            plot_bgcolor: "#efefef;", // Fondo gris claro
            paper_bgcolor: "#ffffff", // Fondo blanco
            font: { color: "black" }, // Fuente negra
            showlegend: false
        });
        return;
    }
    

    let pulseData = uniqueFALines.map((faLine, index) => ({
        x: pulseHistoryX[faLine],
        y: pulseHistoryY[faLine].map(state => state === 1 ? index + 0.5 : index),
        mode: 'lines+markers',
        line: { 
            shape: 'hv', 
            width: 5, 
            color: pulseHistoryY[faLine][pulseHistoryY[faLine].length - 1] === 1 
                ? 'rgba(114,196,149,0.9)' // Verde oscuro para línea activa(114,196,149,0.9)
                : 'rgba(247,168,168,0.9)' // Rojo para línea inactiva(255,126,99,0.9)"rgba(247, 168, 168, 0.9)"
        },
        marker: { 
            size: 6,
            color: pulseHistoryY[faLine].map(state => state === 1 ? 'rgba(114,196,149,0.9)' : 'rgba(255,126,99,0.9)'), // Verde o rojo
        },
        text: pulseHistoryY[faLine].map(state => state === 1 ? "Línea Activa" : "Línea Inactiva"),
        hovertemplate: '%{text}<extra></extra>',
        name: faLine
    }));
    

    let annotations = uniqueFALines.map((faLine, index) => ({
        x: 1,
        xref: "paper",
        y: index,
        yref: "y",
        xanchor: "left",
        yanchor: "middle",
        text: `${formatTime(inactiveTimeByLine[faLine])}`,
        showarrow: false,
        font: { size: 12, color: "black" }, // Texto negro
        align: "left"
    }));    

    let pulseLayout = {
        title: "",
        xaxis: { 
            title: "", 
            color: "black",
            tickfont: { color: "black", size: 12 }
        },
        yaxis: { 
            title: "FA Lines", 
            tickvals: yPositions, 
            ticktext: uniqueFALines, 
            color: "black",
            tickfont: { color: "black", size: 14, weight: "bold" },
            titlefont: { color: "#007399", size: 16 }
        },
        annotations,
        plot_bgcolor: "#ffffff", // Fondo blanco para gráficos
        paper_bgcolor: "#efefef", // Fondo gris claro para todo
        font: { color: "black" },
        showlegend: false,
        height: 500, // Altura fija
        //width: 800,  // Ancho fijo
        autosize: true, // Desactiva ajuste automático
        margin: { t: 50, r: 100, b: 50, l: 50 } // Márgenes constantes
    };
    

    Plotly.react("pulse_chart", pulseData, pulseLayout, { transition: { duration: 300 } });
};

// Sincronizar datos periódicamente con el backend
setInterval(updatePulseHistory, 30000); // Cada 30 segundos
