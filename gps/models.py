from django.db import models


# ─── 1. COORDENADAS EN TIEMPO REAL (ESP32) ────────────────────────────────────
class Coordenada(models.Model):
    """Cada punto GPS recibido del ESP32 en tiempo real."""
    latitud   = models.FloatField()
    longitud  = models.FloatField()
    timestamp = models.DateTimeField(auto_now_add=True)
    velocidad = models.FloatField(null=True, blank=True)  # km/h desde el NEO-6M

    def __str__(self):
        return f"({self.latitud:.4f}, {self.longitud:.4f}) — {self.timestamp}"

    class Meta:
        ordering = ['-timestamp']


# ─── 2. VEHÍCULO ──────────────────────────────────────────────────────────────
class Vehiculo(models.Model):
    """Características del vehículo que afectan el cálculo de rutas."""

    TIPO_CHOICES = [
        ('gasolina', 'Gasolina'),
        ('diesel',   'Diésel'),
        ('electrico','Eléctrico'),
        ('hibrido',  'Híbrido'),
    ]

    nombre              = models.CharField(max_length=100)               # "Mi Tsuru"
    tipo_combustible    = models.CharField(max_length=20, choices=TIPO_CHOICES, default='gasolina')
    rendimiento_kmL     = models.FloatField(default=12.0)                # km por litro
    tanque_litros       = models.FloatField(default=40.0)                # capacidad total
    nivel_combustible   = models.FloatField(default=100.0)               # % actual (del sensor ultrasónico)
    velocidad_promedio  = models.FloatField(default=60.0)                # km/h promedio
    activo              = models.BooleanField(default=True)

    def combustible_disponible_litros(self):
        return (self.nivel_combustible / 100.0) * self.tanque_litros

    def autonomia_km(self):
        return self.combustible_disponible_litros() * self.rendimiento_kmL

    def __str__(self):
        return f"{self.nombre} ({self.tipo_combustible}) — {self.nivel_combustible:.0f}%"


# ─── 3. CASETAS / PEAJES ──────────────────────────────────────────────────────
class Caseta(models.Model):
    """Peaje real georeferenciado. Se usa como penalización en los algoritmos."""

    nombre   = models.CharField(max_length=150)
    latitud  = models.FloatField()
    longitud = models.FloatField()
    costo    = models.FloatField(default=0.0)     # MXN
    activa   = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.nombre} — ${self.costo}"

    class Meta:
        ordering = ['nombre']


# ─── 4. ZONA HORARIA / TRÁFICO ────────────────────────────────────────────────
class ZonaTrafico(models.Model):
    """
    Polígono o punto que representa una zona con tráfico elevado
    en ciertos horarios (hora pico, obras, etc.).
    """
    NIVEL_CHOICES = [
        ('bajo',   'Bajo'),
        ('medio',  'Medio'),
        ('alto',   'Alto'),
        ('critico','Crítico'),
    ]

    nombre          = models.CharField(max_length=150)
    latitud_centro  = models.FloatField()
    longitud_centro = models.FloatField()
    radio_km        = models.FloatField(default=0.5)       # área de efecto
    nivel_trafico   = models.CharField(max_length=10, choices=NIVEL_CHOICES, default='medio')
    hora_inicio     = models.TimeField(null=True, blank=True)   # ej: 07:00
    hora_fin        = models.TimeField(null=True, blank=True)   # ej: 09:00
    activa          = models.BooleanField(default=True)

    # Factor multiplicador de costo: bajo=1.2, medio=1.5, alto=2.0, critico=3.0
    FACTORES = {'bajo': 1.2, 'medio': 1.5, 'alto': 2.0, 'critico': 3.0}

    def factor_costo(self):
        return self.FACTORES.get(self.nivel_trafico, 1.0)

    def __str__(self):
        return f"{self.nombre} ({self.nivel_trafico})"


# ─── 5. RUTA PLANEADA ─────────────────────────────────────────────────────────
class RutaPlaneada(models.Model):
    """Ruta calculada por uno de los algoritmos."""

    ALGORITMO_CHOICES = [
        ('astar',          'A* Manhattan'),
        ('costo_uniforme', 'Costo Uniforme (UCS)'),
        ('genetico',       'Genético Evolutivo'),
    ]

    nombre          = models.CharField(max_length=150)
    algoritmo       = models.CharField(max_length=20, choices=ALGORITMO_CHOICES)
    fecha           = models.DateTimeField(auto_now_add=True)

    # Métricas calculadas
    distancia_km    = models.FloatField(null=True, blank=True)
    tiempo_min      = models.FloatField(null=True, blank=True)       # minutos estimados
    costo_casetas   = models.FloatField(null=True, blank=True)       # MXN
    combustible_L   = models.FloatField(null=True, blank=True)       # litros necesarios
    costo_gasolina  = models.FloatField(null=True, blank=True)       # MXN estimado

    # Relaciones opcionales
    vehiculo        = models.ForeignKey(
                        Vehiculo, on_delete=models.SET_NULL,
                        null=True, blank=True, related_name='rutas')

    activa          = models.BooleanField(default=True)
    es_alterna      = models.BooleanField(default=False)             # ¿es ruta alternativa?
    ruta_principal  = models.ForeignKey(
                        'self', on_delete=models.SET_NULL,
                        null=True, blank=True, related_name='alternas')

    def costo_km(self):
        """Alias para compatibilidad con el código anterior."""
        return self.distancia_km

    def __str__(self):
        return f"{self.nombre} | {self.algoritmo} | {self.distancia_km:.1f} km"

    def estimar_consumo_gasolina(self):
        """
        Calcula cuántos litros se gastarán basándose en la distancia de la ruta
        y el rendimiento definido en el vehículo.
        """
        if self.vehiculo and self.distancia_km:
            # Fórmula: Distancia / Rendimiento (km/L)
            litros = self.distancia_km / self.vehiculo.rendimiento_kmL
            return round(litros, 2)
        return 0.0


# ─── 6. PUNTOS DE LA RUTA ─────────────────────────────────────────────────────
class PuntoRuta(models.Model):
    """Cada nodo (waypoint) de una ruta planeada."""

    TIPO_CHOICES = [
        ('origen',    'Origen'),
        ('intermedio','Intermedio'),
        ('destino',   'Destino'),
        ('caseta',    'Caseta'),
        ('trafico',   'Zona de tráfico'),
    ]

    ruta     = models.ForeignKey(RutaPlaneada, on_delete=models.CASCADE,
                                  related_name='puntos')
    nombre   = models.CharField(max_length=150)
    latitud  = models.FloatField()
    longitud = models.FloatField()
    orden    = models.IntegerField()
    tipo     = models.CharField(max_length=15, choices=TIPO_CHOICES, default='intermedio')

    # Datos OSRM para este segmento (desde el punto anterior hasta este)
    distancia_segmento_km = models.FloatField(null=True, blank=True)
    tiempo_segmento_min   = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f"[{self.orden}] {self.nombre} ({self.tipo})"

    class Meta:
        ordering = ['orden']